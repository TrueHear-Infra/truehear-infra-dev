# Local Docker IP recovery runbook

This runbook recovers the KROPS local-host environment when Docker changes an
internal container IP or a published Kubernetes API port.

The observed incident restarted Vault, RabbitMQ, Flux, and other workload
applications. This procedure applies only to the local CAPD environment.

## Table of contents

- [Observed failure](#observed-failure)
- [Root cause](#root-cause)
- [Safety rules](#safety-rules)
- [Step 1: Take a snapshot](#step-1-take-a-snapshot)
- [Step 2: Verify persistent data](#step-2-verify-persistent-data)
- [Step 3: Check kubeconfig files](#step-3-check-kubeconfig-files)
- [Step 4: Repair management host access](#step-4-repair-management-host-access)
- [Step 5: Regenerate workload host access](#step-5-regenerate-workload-host-access)
- [Step 6: Diagnose the worker endpoint](#step-6-diagnose-the-worker-endpoint)
- [Step 7: Verify a valid API endpoint](#step-7-verify-a-valid-api-endpoint)
- [Step 8: Repair kubelet](#step-8-repair-kubelet)
- [Step 9: Watch workload recovery](#step-9-watch-workload-recovery)
- [Step 10: Unseal Vault](#step-10-unseal-vault)
- [Step 11: Verify RabbitMQ](#step-11-verify-rabbitmq)
- [Step 12: Verify Flux](#step-12-verify-flux)
- [Rollback](#rollback)
- [Why EKS is different](#why-eks-is-different)
- [Recovery checklist](#recovery-checklist)

## Observed failure

The incident produced these symptoms:

- The management kubeconfig referenced an old published localhost port.
- A failed workload export left `local-workload.kubeconfig` at zero bytes.
- Kubernetes then fell back to `http://localhost:8080`.
- After host access was repaired, the workload worker was `NotReady`.
- Workload Pods were terminating or pending.
- The worker had `node.kubernetes.io/unreachable` taints.

The addresses observed in that run were:

| Purpose                            | Observed address  |
| ---------------------------------- | ----------------- |
| Stale worker API endpoint          | `172.26.0.3:6443` |
| Healthy workload control plane     | `172.26.0.7:6443` |
| Stale management host endpoint     | `127.0.0.1:55006` |
| Regenerated workload host endpoint | `127.0.0.1:55012` |

These are historical examples. Docker may assign different addresses during
the next incident. Always discover current values.

## Root cause

```text
Worker kubelet retained an old Docker IP
                |
                v
The address was reassigned to another API endpoint
                |
                v
TLS certificate verification failed
                |
                v
Kubelet stopped renewing its lease
                |
                v
Worker became NotReady and unreachable
                |
                v
Kubernetes evicted workload Pods
```

The decisive kubelet message was:

```text
tls: failed to verify certificate
```

RabbitMQ did not cause the outage. Its deployment occurred near the failure,
but kubelet logs proved that the worker lost its API connection.

## Safety rules

- Do not initialize Vault again.
- Do not delete Vault or RabbitMQ PVCs.
- Do not delete the workload cluster.
- Do not rotate the RabbitMQ Erlang cookie.
- Do not force-delete Pods before diagnosing the node.
- Keep Vault unseal shares and administrative tokens secure.
- Never reuse old IPs without checking current Docker state.

## Step 1: Take a snapshot

Stop any `--watch` command with Ctrl-C. A watch intentionally runs forever.

Check the nodes:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get nodes --output=wide
```

Check all Pods once:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods --all-namespaces --output=wide
```

Check recent events:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get events --all-namespaces \
  --sort-by=.lastTimestamp | tail -60
```

The incident showed `TaintManagerEviction` events for Pods on the worker.

## Step 2: Verify persistent data

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pvc --namespace vault

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pvc --namespace rabbitmq
```

Continue only after confirming these PVCs remain Bound:

- `data-vault-0`, `data-vault-1`, and `data-vault-2`.
- `data-rabbitmq-0`, `data-rabbitmq-1`, and `data-rabbitmq-2`.

Bound PVCs mean the processes are unavailable but persistent data still
exists.

## Step 3: Check kubeconfig files

Check file sizes:

```bash
wc -c \
  .kube/krops-mgmt-host.yaml \
  .kube/krops-mgmt.yaml \
  local-workload.kubeconfig
```

A zero-byte kubeconfig is unusable and can make Kubernetes fall back to
`localhost:8080`.

Inspect endpoints without printing credentials:

```bash
KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
kubectl config view --minify \
  --output='jsonpath={.clusters[0].cluster.server}{"\n"}'

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl config view --minify \
  --output='jsonpath={.clusters[0].cluster.server}{"\n"}'
```

Do not export `KUBECONFIG=local-workload.kubeconfig` while regenerating that
same file. Shell redirection can truncate it before `clusterctl` reads it.

## Step 4: Repair management host access

Find the management API's current published port:

```bash
DOCKER_CONTEXT=default \
docker port local-management-lb 6443/tcp
```

Update only the host endpoint:

```bash
management_endpoint=$(
  DOCKER_CONTEXT=default \
  docker port local-management-lb 6443/tcp | head -1
)

management_port="${management_endpoint##*:}"

kubectl --kubeconfig="$PWD/.kube/krops-mgmt-host.yaml" \
  config set-cluster local-management \
  --server="https://127.0.0.1:${management_port}"
```

Verify management access:

```bash
KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
kubectl get clusters
```

Continue after `local-management` and `local-workload` are returned.

## Step 5: Regenerate workload host access

Use the management kubeconfig explicitly:

```bash
KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
DOCKER_CONTEXT=default \
mise -E local-host run kubeconfigs
```

Verify workload access:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get nodes --output=wide
```

If the control plane is Ready but the worker is NotReady, continue with the
internal endpoint repair.

## Step 6: Diagnose the worker endpoint

Record the current container names from the node output:

```bash
worker="local-workload-md-0-REPLACE-ME"
control_plane="local-workload-REPLACE-ME"
```

Inspect kubelet's configured endpoint:

```bash
DOCKER_CONTEXT=default \
docker exec "$worker" \
  grep -n 'server:' /etc/kubernetes/kubelet.conf
```

Discover the current control-plane IP:

```bash
control_plane_ip=$(
  DOCKER_CONTEXT=default \
  docker inspect "$control_plane" \
    --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
)

printf 'Current control-plane IP: %s\n' "$control_plane_ip"
```

Inspect kubelet logs:

```bash
DOCKER_CONTEXT=default \
docker exec "$worker" \
  journalctl --unit kubelet --since '20 minutes ago' --no-pager | tail -120
```

Look for certificate mismatch, lease renewal, and node-status errors.

## Step 7: Verify a valid API endpoint

Do not disable kubelet TLS verification. Test the control-plane IP using the
workload cluster CA:

```bash
DOCKER_CONTEXT=default \
docker exec "$worker" \
  sh -c "curl \
    --cacert /etc/kubernetes/pki/ca.crt \
    --silent \
    --show-error \
    --output /dev/null \
    --write-out 'HTTP %{http_code}\\n' \
    https://${control_plane_ip}:6443/readyz"
```

Expected result:

```text
HTTP 200
```

If verification fails, inspect the certificate SANs and stop before editing:

```bash
DOCKER_CONTEXT=default \
docker exec "$worker" \
  sh -c "openssl s_client \
    -connect ${control_plane_ip}:6443 </dev/null 2>/dev/null | \
    openssl x509 -noout -subject -ext subjectAltName"
```

During the incident, `172.26.0.7` was covered by the certificate and returned
HTTP 200.

## Step 8: Repair kubelet

Back up the kubelet configuration:

```bash
DOCKER_CONTEXT=default \
docker exec "$worker" \
  cp /etc/kubernetes/kubelet.conf \
  /etc/kubernetes/kubelet.conf.before-ip-repair
```

Replace only the API endpoint:

```bash
DOCKER_CONTEXT=default \
docker exec "$worker" \
  sed -i -E \
  "s#server: https://[^:]+:6443#server: https://${control_plane_ip}:6443#" \
  /etc/kubernetes/kubelet.conf
```

Restart kubelet:

```bash
DOCKER_CONTEXT=default \
docker exec "$worker" systemctl restart kubelet
```

Wait for the worker:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl wait \
  --for=condition=Ready \
  "node/$worker" \
  --timeout=2m
```

## Step 9: Watch workload recovery

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods --all-namespaces --watch
```

Expected order:

1. The worker becomes Ready.
2. Old terminating Pods finish deletion.
3. Controllers create replacement Pods.
4. Persistent volumes attach.
5. Flux and RabbitMQ become ready.
6. Vault remains `0/1 Running` until unsealed.

Press Ctrl-C after the state settles, then take a fresh snapshot:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods --all-namespaces
```

## Step 10: Unseal Vault

Do not run `vault operator init`. Existing PVC data remains initialized.

This example assumes a threshold of three. Use the configured threshold and
the existing shares:

```zsh
for pod in vault-0 vault-1 vault-2; do
  echo "==> Unsealing $pod"

  for share_number in 1 2 3; do
    echo "Enter share $share_number for $pod"

    KUBECONFIG="$PWD/local-workload.kubeconfig" \
    kubectl exec -it --namespace vault "$pod" -- \
      env \
        VAULT_ADDR=https://127.0.0.1:8200 \
        VAULT_SKIP_VERIFY=true \
        vault operator unseal
  done
done
```

This Vault CLI requires an interactive TTY for hidden key entry. Do not pipe
the unseal share.

Verify Vault:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods --namespace vault
```

All three Pods should report `1/1 Running`.

## Step 11: Verify RabbitMQ

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods,pvc --namespace rabbitmq

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace rabbitmq rabbitmq-0 -- \
  rabbitmq-diagnostics cluster_status
```

Confirm three running members, no alarms, and no network partitions.

## Step 12: Verify Flux

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods --namespace flux-system

KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux get kustomizations

KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux get helmreleases --all-namespaces
```

All controllers and required reconciliations should be Ready.

## Rollback

If kubelet fails after the edit, restore the backup:

```bash
DOCKER_CONTEXT=default \
docker exec "$worker" \
  cp /etc/kubernetes/kubelet.conf.before-ip-repair \
  /etc/kubernetes/kubelet.conf

DOCKER_CONTEXT=default \
docker exec "$worker" systemctl restart kubelet
```

Then recheck certificate SANs and Docker addresses. Do not try random IPs.

## Why EKS is different

CAPD runs nodes as Docker containers. Docker's internal addresses and
published ports can change after container or Docker Desktop network events.
This recovery edits an ephemeral local node container.

EKS workers use an AWS-managed Kubernetes API endpoint and AWS networking. The
Docker-IP repair is not an EKS recovery procedure.

## Recovery checklist

- [ ] Vault and RabbitMQ PVCs remain Bound.
- [ ] Management host access uses the current published port.
- [ ] Workload kubeconfig is non-empty and reaches the API.
- [ ] The control-plane node is Ready.
- [ ] Kubelet uses a certificate-valid endpoint.
- [ ] The worker is Ready without unreachable taints.
- [ ] Flux controllers are Running.
- [ ] Vault is unsealed without reinitialization.
- [ ] RabbitMQ lists three running members.
- [ ] Flux Kustomizations and HelmReleases are Ready.
