# Docker Desktop restart recovery runbook

This runbook documents the recovery performed on September 24, 2026, after
enabling Docker Desktop host networking restarted the local KROPS environment.

It applies only to the local CAPD-based management and workload clusters. It
is not an EKS recovery procedure.

For the older, general IP-drift procedure, see
[Local Docker IP recovery runbook](recovery.md).

## Table of contents

- [What happened](#what-happened)
- [Recovery result](#recovery-result)
- [Addresses observed during this incident](#addresses-observed-during-this-incident)
- [Safety rules](#safety-rules)
- [Step 1: Inspect the stopped environment](#step-1-inspect-the-stopped-environment)
- [Step 2: Start the existing containers](#step-2-start-the-existing-containers)
- [Step 3: Diagnose the Kubernetes API servers](#step-3-diagnose-the-kubernetes-api-servers)
- [Step 4: Recover missing API server certificates](#step-4-recover-missing-api-server-certificates)
- [Step 5: Repair the load balancers](#step-5-repair-the-load-balancers)
- [Step 6: Repair host kubeconfig access](#step-6-repair-host-kubeconfig-access)
- [Step 7: Repair the management worker](#step-7-repair-the-management-worker)
- [Step 8: Recover the CAPI controllers](#step-8-recover-the-capi-controllers)
- [Step 9: Repair CAPI cluster endpoints](#step-9-repair-capi-cluster-endpoints)
- [Step 10: Verify Flux and storage](#step-10-verify-flux-and-storage)
- [Step 11: Recover RabbitMQ](#step-11-recover-rabbitmq)
- [Step 12: Unseal Vault](#step-12-unseal-vault)
- [Step 13: Verify the complete environment](#step-13-verify-the-complete-environment)
- [Troubleshooting map](#troubleshooting-map)
- [Repository changes made](#repository-changes-made)
- [Final checklist](#final-checklist)

## What happened

Enabling Docker Desktop host networking restarted the Docker virtual machine.
The KROPS containers still existed, and the persistent volumes still contained
their data, but several runtime addresses and published ports changed.

The restart caused several related failures:

1. The management and workload API server certificates were removed during
   container startup.
2. Certificate regeneration failed because `/kind/kubeadm.conf` was missing.
3. The API server static Pods could not start without
   `/etc/kubernetes/pki/apiserver.crt`.
4. The CAPD load balancers retained stale backend IP addresses.
5. The management worker retained a stale Kubernetes API endpoint.
6. CAPI Cluster objects and generated kubeconfig Secrets retained old load
   balancer addresses.
7. Vault restarted in its normal sealed state.
8. RabbitMQ exposed a restart bug in its config init container.

The main API server error was:

```text
open /etc/kubernetes/pki/apiserver.crt: no such file or directory
```

The management worker error was:

```text
dial tcp 172.26.0.5:6443: connect: connection refused
```

The RabbitMQ init error was:

```text
cp: cannot create regular file '/etc/rabbitmq/rabbitmq.conf': Permission denied
```

## Recovery result

The existing environment was recovered without deleting either cluster or any
persistent volume.

The final state was:

| Component             | Final state                                      |
| --------------------- | ------------------------------------------------ |
| Management nodes      | `2/2 Ready`                                      |
| Workload nodes        | `2/2 Ready`                                      |
| CAPI clusters         | Management and workload `Available=True`         |
| Flux controllers      | All Running                                      |
| Local path provisioner | Running                                         |
| Redis                 | `6/6` Pods Running                               |
| RabbitMQ              | `3/3` Pods Running with no partitions or alarms  |
| Vault                 | Running; manual unsealing required after restart |

## Addresses observed during this incident

These values are historical evidence. Do not reuse them without inspecting the
current Docker environment.

| Component                    | Address after restart       |
| ---------------------------- | --------------------------- |
| Management control plane     | `172.26.0.4:6443`           |
| Management worker            | `172.26.0.5`                |
| Management load balancer     | `172.26.0.6:6443`           |
| Management host API          | `127.0.0.1:55006`           |
| Workload control plane       | `172.26.0.2:6443`           |
| Workload worker              | `172.26.0.3`                |
| Workload load balancer       | `172.26.0.7:6443`           |
| Workload host API            | `127.0.0.1:55008`           |
| Local OCI registry           | `127.0.0.1:5001`            |

The stale CAPI endpoints were `172.26.0.5:6443` for management and
`172.26.0.3:6443` for workload. After Docker restarted, those addresses
belonged to worker containers instead of load balancers.

## Safety rules

- Do not run bootstrap again over the existing environment.
- Do not run teardown during recovery.
- Do not delete Vault, RabbitMQ, or Redis PVCs.
- Do not initialize Vault again.
- Do not rotate the RabbitMQ Erlang cookie.
- Do not regenerate the Kubernetes certificate authorities.
- Regenerate only a missing API server leaf certificate.
- Back up every runtime configuration before editing it.
- Discover current IP addresses and ports instead of copying historical ones.
- Never write Vault unseal shares, root tokens, or passwords into this repo.

## Step 1: Inspect the stopped environment

Work from the repository root:

```bash
cd /Users/sameer/sameer/truehear-infra-dev
```

List all local cluster containers, including stopped containers:

```bash
DOCKER_CONTEXT=default docker ps --all \
  --filter network=kind \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

Confirm that the management, workload, load-balancer, and registry containers
still exist. Their existence means this is a recovery, not a new bootstrap.

Inspect the persistent volume claims as soon as either API becomes available:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pvc --namespace vault

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pvc --namespace rabbitmq

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pvc --namespace redis
```

Do not continue with destructive actions. Bound PVCs mean the application data
still exists.

## Step 2: Start the existing containers

In this incident, the load balancers and OCI registry did not restart
automatically:

```bash
DOCKER_CONTEXT=default docker start \
  local-management-lb \
  local-workload-lb \
  krops-registry
```

Start only containers that already exist. Do not create replacements.

List the current container addresses:

```bash
DOCKER_CONTEXT=default docker inspect \
  --format '{{.Name}} {{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
  $(DOCKER_CONTEXT=default docker ps --all --quiet --filter network=kind)
```

List the current host ports:

```bash
DOCKER_CONTEXT=default docker port local-management-lb 6443/tcp
DOCKER_CONTEXT=default docker port local-workload-lb 6443/tcp
DOCKER_CONTEXT=default docker port krops-registry 5000/tcp
```

Record these values. They may change again if a load-balancer container is
restarted.

## Step 3: Diagnose the Kubernetes API servers

Inspect the control-plane container logs:

```bash
DOCKER_CONTEXT=default docker logs \
  --tail 100 local-management-CONTROL-PLANE

DOCKER_CONTEXT=default docker logs \
  --tail 100 local-workload-CONTROL-PLANE
```

Check the API server certificate files:

```bash
DOCKER_CONTEXT=default docker exec local-management-CONTROL-PLANE \
  ls -l /etc/kubernetes/pki/apiserver.crt \
  /etc/kubernetes/pki/apiserver.key \
  /etc/kubernetes/pki/ca.crt \
  /etc/kubernetes/pki/ca.key
```

Repeat for the workload control plane.

Use Step 4 only when the API server certificate or key is missing and the
existing CA certificate and CA key are present.

If the API server certificate exists, skip Step 4 and diagnose the static Pod
logs instead.

## Step 4: Recover missing API server certificates

This step reuses the existing cluster CA. It does not create a new CA.

First, record the following values for each cluster:

- Cluster name.
- Control-plane container name and current IP.
- Load-balancer name and current IP.
- Previous API endpoint, if clients still reference it.
- Kubernetes Service IP, normally `10.128.0.1` in this environment.

Create a temporary kubeadm configuration for management:

```yaml
apiVersion: kubeadm.k8s.io/v1beta4
kind: ClusterConfiguration
clusterName: local-management
controlPlaneEndpoint: local-management-lb:6443
networking:
  dnsDomain: cluster.local
  serviceSubnet: 10.128.0.0/12
apiServer:
  certSANs:
    - kubernetes
    - kubernetes.default
    - kubernetes.default.svc
    - kubernetes.default.svc.cluster.local
    - local-management-lb
    - local-management-CONTROL-PLANE
    - 10.128.0.1
    - CURRENT_CONTROL_PLANE_IP
    - CURRENT_LOAD_BALANCER_IP
    - 127.0.0.1
    - PREVIOUS_API_IP
```

Save it outside the repository:

```bash
$EDITOR /tmp/local-management-kubeadm-recovery.yaml
```

Copy it into the control-plane container and regenerate only the API server
leaf certificate:

```bash
DOCKER_CONTEXT=default docker cp \
  /tmp/local-management-kubeadm-recovery.yaml \
  local-management-CONTROL-PLANE:/tmp/kubeadm-recovery.yaml

DOCKER_CONTEXT=default docker exec local-management-CONTROL-PLANE \
  kubeadm init phase certs apiserver \
  --config /tmp/kubeadm-recovery.yaml

DOCKER_CONTEXT=default docker exec local-management-CONTROL-PLANE \
  systemctl restart kubelet
```

Repeat with workload-specific names and addresses.

Verify the certificate SANs:

```bash
DOCKER_CONTEXT=default docker exec local-management-CONTROL-PLANE \
  openssl x509 \
  -in /etc/kubernetes/pki/apiserver.crt \
  -noout \
  -ext subjectAltName
```

The current control plane and load balancer must appear in the certificate.

## Step 5: Repair the load balancers

The restarted HAProxy containers pointed their backend at their own new IP
instead of the control-plane IP.

Copy each HAProxy configuration to the host:

```bash
DOCKER_CONTEXT=default docker cp \
  local-management-lb:/usr/local/etc/haproxy/haproxy.cfg \
  /tmp/local-management-haproxy.cfg

DOCKER_CONTEXT=default docker cp \
  local-workload-lb:/usr/local/etc/haproxy/haproxy.cfg \
  /tmp/local-workload-haproxy.cfg
```

Inspect the backend entries:

```bash
grep -n 'server' /tmp/local-management-haproxy.cfg
grep -n 'server' /tmp/local-workload-haproxy.cfg
```

Edit only the backend addresses so they point to the current control-plane
containers. During this incident:

```text
management backend: 172.26.0.4:6443
workload backend:   172.26.0.2:6443
```

Copy the repaired files back and restart only the load balancers:

```bash
DOCKER_CONTEXT=default docker cp \
  /tmp/local-management-haproxy.cfg \
  local-management-lb:/usr/local/etc/haproxy/haproxy.cfg

DOCKER_CONTEXT=default docker cp \
  /tmp/local-workload-haproxy.cfg \
  local-workload-lb:/usr/local/etc/haproxy/haproxy.cfg

DOCKER_CONTEXT=default docker restart local-management-lb
DOCKER_CONTEXT=default docker restart local-workload-lb
```

Rediscover the published ports after restarting the load balancers:

```bash
DOCKER_CONTEXT=default docker port local-management-lb 6443/tcp
DOCKER_CONTEXT=default docker port local-workload-lb 6443/tcp
```

## Step 6: Repair host kubeconfig access

Point the management host kubeconfig at the current published port:

```bash
kubectl --kubeconfig="$PWD/.kube/krops-mgmt-host.yaml" \
  config set-cluster local-management \
  --server=https://127.0.0.1:MANAGEMENT_HOST_PORT
```

Point the workload host kubeconfig at its current published port:

```bash
kubectl --kubeconfig="$PWD/local-workload.kubeconfig" \
  config set-cluster local-workload \
  --server=https://127.0.0.1:WORKLOAD_HOST_PORT
```

Verify both APIs:

```bash
KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" kubectl get nodes
KUBECONFIG="$PWD/local-workload.kubeconfig" kubectl get nodes
```

During this incident, the final host ports were `55006` for management and
`55008` for workload.

## Step 7: Repair the management worker

The management worker kubelet still used the stale endpoint
`https://172.26.0.5:6443`. That address belonged to the worker itself after
Docker restarted.

Inspect the worker kubelet endpoint:

```bash
DOCKER_CONTEXT=default docker exec MANAGEMENT_WORKER \
  grep 'server:' /etc/kubernetes/kubelet.conf
```

Back up and update the kubelet configuration inside the ephemeral node:

```bash
DOCKER_CONTEXT=default docker exec MANAGEMENT_WORKER \
  cp /etc/kubernetes/kubelet.conf \
  /etc/kubernetes/kubelet.conf.before-docker-restart-recovery

DOCKER_CONTEXT=default docker exec MANAGEMENT_WORKER \
  sed -i \
  's#https://OLD_API_IP:6443#https://CURRENT_MANAGEMENT_LB_IP:6443#g' \
  /etc/kubernetes/kubelet.conf

DOCKER_CONTEXT=default docker exec MANAGEMENT_WORKER \
  systemctl restart kubelet
```

Repair the management kube-proxy ConfigMap if it contains the same stale
endpoint:

```bash
KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
kubectl get configmap kube-proxy \
  --namespace kube-system \
  --output=yaml > /tmp/management-kube-proxy-before-recovery.yaml

cp /tmp/management-kube-proxy-before-recovery.yaml \
  /tmp/management-kube-proxy-recovery.yaml

sed -i '' \
  's#https://OLD_API_IP:6443#https://CURRENT_MANAGEMENT_LB_IP:6443#g' \
  /tmp/management-kube-proxy-recovery.yaml

KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
kubectl apply -f /tmp/management-kube-proxy-recovery.yaml

KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
kubectl rollout restart daemonset/kube-proxy \
  --namespace kube-system
```

Wait for the management worker:

```bash
KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
kubectl wait \
  --for=condition=Ready \
  node/MANAGEMENT_WORKER \
  --timeout=3m
```

The workload kubelet and kube-proxy already referenced the correct new
workload load balancer during this incident, so they did not require edits.

## Step 8: Recover the CAPI controllers

The CAPI controller Pods entered exponential crash backoff while management
networking was unavailable. They are stateless and safe to restart after the
network is repaired:

```bash
KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
kubectl rollout restart deployment \
  --namespace capi-system

KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
kubectl rollout status deployment \
  --namespace capi-system \
  --timeout=5m
```

Verify them:

```bash
KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
kubectl get pods --namespace capi-system
```

## Step 9: Repair CAPI cluster endpoints

CAPI still referenced the old load-balancer addresses after its controllers
recovered.

Inspect all three endpoint layers:

```bash
KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
kubectl get devclusters --namespace default \
  --output=custom-columns='NAME:.metadata.name,HOST:.spec.controlPlaneEndpoint.host'

KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
kubectl get clusters --namespace default \
  --output=custom-columns='NAME:.metadata.name,HOST:.spec.controlPlaneEndpoint.host'

for cluster in local-management local-workload; do
  printf '%s: ' "$cluster"
  KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
  kubectl get secret "${cluster}-kubeconfig" \
    --namespace default \
    --output='jsonpath={.data.value}' | \
    base64 --decode | grep 'server:'
done
```

Find the generated DevCluster names:

```bash
KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
kubectl get clusters --namespace default \
  --output=custom-columns='CLUSTER:.metadata.name,DEVCLUSTER:.spec.infrastructureRef.name'
```

Patch the DevCluster resources with the current load-balancer IPs:

```bash
KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
kubectl patch devcluster MANAGEMENT_DEVCLUSTER \
  --namespace default \
  --type=merge \
  --patch='{"spec":{"controlPlaneEndpoint":{"host":"CURRENT_MANAGEMENT_LB_IP","port":6443}}}'

KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
kubectl patch devcluster WORKLOAD_DEVCLUSTER \
  --namespace default \
  --type=merge \
  --patch='{"spec":{"controlPlaneEndpoint":{"host":"CURRENT_WORKLOAD_LB_IP","port":6443}}}'
```

The DevCluster changes did not automatically refresh the creation-time Cluster
fields during this incident. Align those derived fields:

```bash
KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
kubectl patch cluster local-management \
  --namespace default \
  --type=merge \
  --patch='{"spec":{"controlPlaneEndpoint":{"host":"CURRENT_MANAGEMENT_LB_IP","port":6443}}}'

KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
kubectl patch cluster local-workload \
  --namespace default \
  --type=merge \
  --patch='{"spec":{"controlPlaneEndpoint":{"host":"CURRENT_WORKLOAD_LB_IP","port":6443}}}'
```

Update each generated kubeconfig Secret without printing its credentials:

```bash
repair_capi_kubeconfig() {
  local cluster="$1"
  local host="$2"
  local file
  local kubeconfig_cluster
  local encoded

  file=$(mktemp "/tmp/${cluster}.recovery.XXXXXX")
  chmod 600 "$file"

  KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
  kubectl get secret "${cluster}-kubeconfig" \
    --namespace default \
    --output='jsonpath={.data.value}' | \
    base64 --decode > "$file"

  kubeconfig_cluster=$(kubectl --kubeconfig="$file" \
    config view --minify \
    --output='jsonpath={.contexts[0].context.cluster}')

  kubectl --kubeconfig="$file" \
    config set-cluster "$kubeconfig_cluster" \
    --server="https://${host}:6443" >/dev/null

  encoded=$(base64 < "$file" | tr -d '\n')

  KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
  kubectl patch secret "${cluster}-kubeconfig" \
    --namespace default \
    --type=merge \
    --patch="{\"data\":{\"value\":\"${encoded}\"}}" >/dev/null

  rm -f "$file"
}

repair_capi_kubeconfig local-management CURRENT_MANAGEMENT_LB_IP
repair_capi_kubeconfig local-workload CURRENT_WORKLOAD_LB_IP
unset -f repair_capi_kubeconfig
```

Wait for CAPI to report both clusters Available:

```bash
KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
kubectl get clusters --namespace default --watch
```

Stop the watch with Ctrl-C after both rows show `AVAILABLE True`.

## Step 10: Verify Flux and storage

Check the workload Flux controllers:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods --namespace flux-system
```

Check Flux reconciliation:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux get kustomizations

KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux get helmreleases --all-namespaces
```

Check local storage:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods --namespace local-path-storage

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get storageclass
```

If Flux reports a timeout to a `source-controller` ClusterIP, inspect and
repair the workload kube-proxy endpoint using the general
[recovery runbook](recovery.md#step-9-repair-kube-proxy).

## Step 11: Recover RabbitMQ

### Why RabbitMQ did not restart

The RabbitMQ chart uses an `emptyDir` for generated configuration. The
directory survived a container restart within the existing Pod. Files from the
first init run were owned by RabbitMQ, and the restricted root init container
could not overwrite them.

The chart was corrected to remove only the generated files before recreating
them:

```yaml
rm -f \
  /etc/rabbitmq/rabbitmq.conf \
  /etc/rabbitmq/enabled_plugins \
  /etc/rabbitmq/inter_node_tls.config \
  /etc/rabbitmq/rabbitmq-env.conf
```

The chart version and OCI source reference were bumped from `0.1.0` to
`0.1.1`.

Publish the corrected chart and local-host artifact:

```bash
mise -E local-host run rabbitmq-chart-push
mise -E local-host run oci-push
```

Reconcile Flux:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux reconcile source oci \
  truehear-rabbitmq-chart \
  --namespace rabbitmq

KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux reconcile source oci \
  flux-system \
  --namespace flux-system

KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux reconcile kustomization \
  flux-system \
  --namespace flux-system

KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux reconcile kustomization \
  rabbitmq \
  --namespace flux-system

KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux reconcile helmrelease \
  rabbitmq \
  --namespace rabbitmq \
  --with-source
```

The RabbitMQ StatefulSet uses the `OnDelete` update strategy. Flux updates the
StatefulSet, but Kubernetes does not automatically replace existing Pods.

Confirm the update strategy and revisions:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get statefulset rabbitmq \
  --namespace rabbitmq \
  --output='jsonpath={.spec.updateStrategy}{"\n"}{.status.currentRevision}{" -> "}{.status.updateRevision}{"\n"}'
```

Rotate the Pods from highest ordinal to lowest. PVCs are retained:

```bash
for ordinal in 2 1 0; do
  KUBECONFIG="$PWD/local-workload.kubeconfig" \
  kubectl delete pod "rabbitmq-${ordinal}" \
    --namespace rabbitmq

  KUBECONFIG="$PWD/local-workload.kubeconfig" \
  kubectl wait \
    --for=jsonpath='{.status.phase}'=Running \
    "pod/rabbitmq-${ordinal}" \
    --namespace rabbitmq \
    --timeout=4m
done
```

Wait for all three members:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl wait \
  --for=condition=Ready \
  pod/rabbitmq-0 \
  pod/rabbitmq-1 \
  pod/rabbitmq-2 \
  --namespace rabbitmq \
  --timeout=8m
```

Verify the RabbitMQ cluster:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace rabbitmq rabbitmq-0 -- \
  rabbitmq-diagnostics cluster_status
```

The expected result is three disk nodes, three running nodes, no alarms, and
no network partitions.

## Step 12: Unseal Vault

Vault sealing after a restart is expected when auto-unseal is not configured.
Do not initialize Vault again.

The following one-liner securely prompts for three existing unseal shares once
and applies them to all three Pods:

```bash
read -s "k1?Unseal share 1: "; echo; read -s "k2?Unseal share 2: "; echo; read -s "k3?Unseal share 3: "; echo; for pod in vault-0 vault-1 vault-2; do echo "Unsealing ${pod}"; for key in "$k1" "$k2" "$k3"; do printf '%s\n' "$key" | KUBECONFIG="$PWD/local-workload.kubeconfig" kubectl exec -i -n vault "$pod" -- sh -ec 'IFS= read -r key; VAULT_ADDR=https://127.0.0.1:8200 VAULT_SKIP_VERIFY=true vault operator unseal "$key" >/dev/null'; done; done; unset k1 k2 k3
```

The command uses hidden terminal input and does not place the unseal shares in
shell history. Never paste shares into a committed file.

Verify Vault:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods --namespace vault
```

All three Pods should show `1/1 Running`.

## Step 13: Verify the complete environment

Verify management:

```bash
KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" kubectl get nodes
KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" kubectl get clusters
KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" kubectl get pods --all-namespaces
```

Verify workload nodes and applications:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" kubectl get nodes
KUBECONFIG="$PWD/local-workload.kubeconfig" kubectl get pods --all-namespaces
```

Verify the stateful workloads:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods,pvc --namespace vault

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods,pvc --namespace rabbitmq

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods,pvc --namespace redis
```

Verify Flux:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux get all --all-namespaces
```

## Troubleshooting map

| Symptom                                          | Likely cause                              | Recovery step |
| ------------------------------------------------ | ----------------------------------------- | ------------- |
| Docker containers exist but are stopped          | Docker Desktop restarted                  | Step 2        |
| API server reports missing `apiserver.crt`        | CAPD entrypoint certificate failure       | Step 4        |
| Load balancer accepts traffic but API is down     | HAProxy backend points to the wrong IP    | Step 5        |
| Host kubeconfig reports connection refused        | Published Docker port changed             | Step 6        |
| Management worker is `NotReady`                   | Kubelet uses a stale API address          | Step 7        |
| CAPI controllers repeatedly restart               | They entered backoff during network loss  | Step 8        |
| CAPI logs show old `.3` or `.5` endpoint          | CAPI runtime endpoints are stale          | Step 9        |
| Flux cannot download a source-controller archive  | Workload Service routing is stale         | Step 10       |
| RabbitMQ init reports permission denied            | Generated files cannot be overwritten     | Step 11       |
| Vault Pods show `0/1 Running`                     | Vault is sealed after restart             | Step 12       |

## Repository changes made

The recovery required one persistent Git-managed correction:

- `workload/local-host/rabbitmq/chart/templates/statefulset.yaml` removes old
  generated config files before recreating them.
- `workload/local-host/rabbitmq/chart/Chart.yaml` changes the chart version to
  `0.1.1`.
- `workload/local-host/rabbitmq/chart-source.yaml` tells Flux to pull chart
  version `0.1.1`.

The certificate, HAProxy, kubelet, kube-proxy, DevCluster, Cluster, kubeconfig,
and host-port repairs were local runtime recovery actions. They were not
application manifests and were not committed as desired steady state.

## Final checklist

- [ ] Existing containers were recovered instead of recreated.
- [ ] Vault, RabbitMQ, and Redis PVCs remain Bound.
- [ ] Both API server certificates exist and contain current SANs.
- [ ] Both load balancers point to their current control-plane containers.
- [ ] Host kubeconfigs use current published localhost ports.
- [ ] Both management nodes are Ready.
- [ ] Both workload nodes are Ready.
- [ ] CAPI controllers are Running.
- [ ] Both CAPI Cluster resources show Available.
- [ ] Flux controllers and reconciliations are Ready.
- [ ] The storage provisioner is Running.
- [ ] Redis has six Ready Pods.
- [ ] RabbitMQ has three Ready Pods and no cluster partitions.
- [ ] Vault has been unsealed and has three Ready Pods.
- [ ] No PVC, cluster, or application data was deleted.
