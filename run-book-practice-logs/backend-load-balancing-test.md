# Local backend load-balancing test

This runbook records how traffic was sent through the Kubernetes backend
Service and observed across three TrueHear backend Pods.

## Table of contents

- [Goal](#goal)
- [What this test proves](#what-this-test-proves)
- [Traffic flow](#traffic-flow)
- [Prerequisites](#prerequisites)
- [Step 1: Verify the backend Pods](#step-1-verify-the-backend-pods)
- [Step 2: Verify the Service endpoints](#step-2-verify-the-service-endpoints)
- [Step 3: Watch each Pod separately](#step-3-watch-each-pod-separately)
- [Step 4: Generate test traffic](#step-4-generate-test-traffic)
- [Step 5: Confirm distribution](#step-5-confirm-distribution)
- [Why the first test appeared empty](#why-the-first-test-appeared-empty)
- [Important behavior](#important-behavior)
- [Troubleshooting](#troubleshooting)
- [Result](#result)

## Goal

Confirm that `Service/truehear-backend` discovers all ready backend Pods and
distributes new client connections across them.

The test uses three separate log terminals so it is easy to see which Pod
received each request.

## What this test proves

The test proves that:

- the Service selector matches the backend Pods;
- Kubernetes publishes the ready Pods through an EndpointSlice;
- the Service DNS name is reachable from inside the cluster;
- new connections are distributed across multiple ready Pods;
- scaling the Deployment does not require manual Service registration.

This test does not prove internet-facing AWS load balancing. The local Service
is a `ClusterIP`, which is internal to Kubernetes.

## Traffic flow

```text
Temporary curl Pod
        |
        | http://truehear-backend:8887
        v
Service/truehear-backend
        |
        | selector: app.kubernetes.io/name=truehear-backend
        v
EndpointSlice
├── backend Pod 1
├── backend Pod 2
└── backend Pod 3
```

The Service has no hardcoded Pod names or IP addresses. Kubernetes updates the
EndpointSlice whenever Pods become ready, unready, are replaced, or are scaled.

## Prerequisites

Run commands from the repository root:

```bash
cd /Users/sameer/sameer/truehear-infra-dev
```

Confirm workload-cluster access:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get nodes
```

Confirm Flux has applied the backend:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux get kustomizations
```

The `backend` Kustomization must be ready.

## Step 1: Verify the backend Pods

List the current backend Pods:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods \
  --namespace backend \
  --selector app.kubernetes.io/name=truehear-backend
```

For the three-replica test, expect three `1/1 Running` Pods. Deployment Pod
names contain a ReplicaSet hash and random suffix, so they may differ on every
run.

## Step 2: Verify the Service endpoints

Inspect the Service:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get service truehear-backend \
  --namespace backend \
  --output wide
```

Inspect its EndpointSlice:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get endpointslice \
  --namespace backend \
  --selector kubernetes.io/service-name=truehear-backend \
  --output wide
```

The EndpointSlice should contain three ready addresses. These are the current
backend Pod IP addresses selected by the Service.

## Step 3: Watch each Pod separately

Open three terminals. Each command discovers one current Pod by index, then
follows only that Pod's logs.

### Terminal 1

```bash
backend_pod=$(KUBECONFIG="$PWD/local-workload.kubeconfig" \
  kubectl get pods \
    --namespace backend \
    --selector app.kubernetes.io/name=truehear-backend \
    --output jsonpath='{.items[0].metadata.name}')

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl logs \
  --namespace backend \
  "pod/$backend_pod" \
  --follow \
  --tail=0 |
rg --line-buffered --invert-match 'health/(ready|live)'
```

### Terminal 2

```bash
backend_pod=$(KUBECONFIG="$PWD/local-workload.kubeconfig" \
  kubectl get pods \
    --namespace backend \
    --selector app.kubernetes.io/name=truehear-backend \
    --output jsonpath='{.items[1].metadata.name}')

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl logs \
  --namespace backend \
  "pod/$backend_pod" \
  --follow \
  --tail=0 |
rg --line-buffered --invert-match 'health/(ready|live)'
```

### Terminal 3

```bash
backend_pod=$(KUBECONFIG="$PWD/local-workload.kubeconfig" \
  kubectl get pods \
    --namespace backend \
    --selector app.kubernetes.io/name=truehear-backend \
    --output jsonpath='{.items[2].metadata.name}')

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl logs \
  --namespace backend \
  "pod/$backend_pod" \
  --follow \
  --tail=0 |
rg --line-buffered --invert-match 'health/(ready|live)'
```

The filter hides Kubernetes readiness and liveness probes. It does not stop
the probes or change Pod health.

## Step 4: Generate test traffic

Open a fourth terminal and start a temporary curl Pod inside the backend
namespace:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl run backend-load-test \
  --namespace backend \
  --rm \
  --stdin \
  --restart=Never \
  --image=curlimages/curl:8.12.1 \
  --command -- sh -c '
    i=1
    while [ "$i" -le 60 ]; do
      status=$(curl --silent --show-error \
        --output /dev/null \
        --write-out "%{http_code}" \
        --header "Connection: close" \
        "http://truehear-backend:8887/api/truehear/v2/health?request=${i}")

      echo "request=${i} status=${status}"
      i=$((i + 1))
    done
  '
```

Expected output contains 60 successful responses:

```text
request=1 status=200
request=2 status=200
...
request=60 status=200
```

`--rm` deletes the temporary load-test Pod when the command finishes.

## Step 5: Confirm distribution

Watch the three log terminals while the requests run. Requests to:

```text
/api/truehear/v2/health?request=<number>
```

should appear in all three terminals.

The counts do not need to be exactly 20 requests per Pod. Kubernetes distributes
new connections, but does not guarantee strict round-robin equality.

Stop each log stream with `Ctrl-C` after the test.

## Why the first test appeared empty

The first traffic command called:

```text
/api/truehear/v2/health/live
```

The log commands were filtering:

```text
health/(ready|live)
```

The test requests were therefore successfully hidden along with Kubernetes
liveness traffic. The corrected test calls `/health`, while the filter hides
only `/health/live` and `/health/ready`.

Use this separation:

```text
/health/ready -> Kubernetes readiness probe, hidden
/health/live  -> Kubernetes liveness probe, hidden
/health       -> Manual load-balancing test, visible
```

## Important behavior

### Kubernetes balances connections

The Service normally selects a backend for each new network connection. A
long-lived HTTP keep-alive connection can remain attached to one Pod.

The test sends:

```text
Connection: close
```

This forces each request to open a new connection and gives the Service a new
opportunity to select an endpoint.

### Port-forward is not the correct balancing test

This command is useful for reaching the application from the Mac:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl port-forward \
  --namespace backend \
  service/truehear-backend \
  8887:8887
```

However, `kubectl port-forward service/...` normally resolves the Service to
one backing Pod and forwards directly to it. Repeated requests through that
tunnel do not prove Service-level distribution.

The load test therefore runs inside Kubernetes and connects to the Service DNS
name.

## Troubleshooting

### No traffic appears in any log terminal

Confirm the traffic generator calls `/health`, not `/health/live`.

Confirm the requests return `status=200`. If not, inspect the temporary Pod
before using `--rm`, or test Service DNS from a temporary shell.

### Only one Pod receives requests

Confirm the Service has three ready endpoints:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get endpointslice \
  --namespace backend \
  --selector kubernetes.io/service-name=truehear-backend \
  --output wide
```

Confirm the test includes `Connection: close`. Without it, connection reuse
can keep requests on one Pod.

### A log terminal reports that its Pod does not exist

Deployment Pods are replaceable. List the current names and restart the log
commands:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods --namespace backend
```

### Readiness probes fill the logs

The kubelet calls `/health/ready` every ten seconds for each backend Pod. The
liveness probe calls `/health/live` every twenty seconds. These are expected
infrastructure requests.

Keep the probes enabled and filter them only from the local log display.

## Result

The completed test sent 60 independent HTTP connections through
`Service/truehear-backend`. Requests appeared across all three backend Pod log
streams.

This confirmed the complete local path:

```text
In-cluster client
  -> stable Kubernetes Service
  -> automatically maintained EndpointSlice
  -> three ready backend Pods
```
