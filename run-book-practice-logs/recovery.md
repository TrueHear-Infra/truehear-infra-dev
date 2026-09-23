# Local Docker IP recovery runbook

This runbook recovers the KROPS local-host environment when Docker changes an
internal container IP or a published Kubernetes API port.

The observed incident restarted Vault, RabbitMQ, Flux, and other workload
applications. This procedure applies only to the local CAPD environment.

## Table of contents

- [Observed failure](#observed-failure)
- [Root cause](#root-cause)
- [Choose the recovery path](#choose-the-recovery-path)
- [Safety rules](#safety-rules)
- [Step 1: Take a snapshot](#step-1-take-a-snapshot)
- [Step 2: Verify persistent data](#step-2-verify-persistent-data)
- [Step 3: Check kubeconfig files](#step-3-check-kubeconfig-files)
- [Step 4: Repair management host access](#step-4-repair-management-host-access)
- [Step 5: Regenerate workload host access](#step-5-regenerate-workload-host-access)
- [Step 6: Diagnose the worker endpoint](#step-6-diagnose-the-worker-endpoint)
- [Step 7: Verify a valid API endpoint](#step-7-verify-a-valid-api-endpoint)
- [Step 8: Repair kubelet](#step-8-repair-kubelet)
- [Step 9: Repair kube-proxy](#step-9-repair-kube-proxy)
- [Step 10: Watch workload recovery](#step-10-watch-workload-recovery)
- [Step 11: Unseal Vault](#step-11-unseal-vault)
- [Step 12: Verify RabbitMQ](#step-12-verify-rabbitmq)
- [Step 13: Verify Flux](#step-13-verify-flux)
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
- After kubelet recovery, Flux still timed out when accessing the
  source-controller ClusterIP.

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

Docker can assign a different internal IP to the workload control plane. Two
node components can retain the old address:

```text
Old control-plane Docker IP
        |
        +--> kubelet cannot reach the API
        |        |
        |        +--> worker becomes NotReady
        |             and Pods are evicted
        |
        +--> kube-proxy cannot watch Services
                 |
                 +--> ClusterIP routing becomes stale
                      and Flux cannot fetch artifacts
```

The decisive kubelet message was:

```text
tls: failed to verify certificate
```

RabbitMQ did not cause the outage. Its deployment occurred near the failure,
but kubelet logs proved that the worker lost its API connection.

## Choose the recovery path

Use the symptom and log message to decide which component to repair. More than
one component can retain the same stale address, so complete every matching
row.

| Symptom                                                            | Evidence                                          | Recovery                                             |
| ------------------------------------------------------------------ | ------------------------------------------------- | ---------------------------------------------------- |
| Host command reports connection refused on `127.0.0.1:<port>`      | The load balancer publishes a different port      | Follow Steps 3 through 5                             |
| Command falls back to `http://localhost:8080`                      | The selected kubeconfig is missing or empty       | Follow Steps 3 through 5                             |
| Worker is `NotReady`                                               | Kubelet logs show TLS errors for an old API IP    | Follow Steps 6 through 8                             |
| Nodes are Ready but ClusterIP Services time out                    | kube-proxy logs show TLS errors for an old API IP | Follow Step 9                                        |
| Flux reports `failed to download archive` from `source-controller` | The source-controller ClusterIP times out         | Check kube-proxy, then follow Step 9                 |
| Vault Pods are `0/1 Running` after recovery                        | Vault status reports `Sealed: true`               | Follow Step 11; never initialize again               |
| RabbitMQ Pods restart while PVCs remain Bound                      | The node or Service network was unavailable       | Repair the underlying component, then follow Step 12 |

The important distinction is:

- `kubelet` keeps the node and its Pods connected to Kubernetes.
- `kube-proxy` keeps Kubernetes Service addresses, including ClusterIPs,
  working on each node.

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

## Step 9: Repair kube-proxy

Run this step when nodes are Ready but ClusterIP Services time out. A typical
Flux error is:

```text
failed to download archive: dial tcp <source-controller-cluster-ip>:80:
i/o timeout
```

Confirm kube-proxy is using a stale address:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get configmap kube-proxy \
  --namespace kube-system \
  --output='jsonpath={.data.kubeconfig\.conf}'

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl logs \
  --namespace kube-system \
  --selector=k8s-app=kube-proxy \
  --tail=30 \
  --prefix
```

Look for an old `server: https://<ip>:6443` value and repeated TLS errors.
Compare that address with `control_plane_ip` discovered in Step 6.

If this shell does not already have those variables, discover them again:

```bash
control_plane=$(
  KUBECONFIG="$PWD/local-workload.kubeconfig" \
  kubectl get nodes \
    --selector='node-role.kubernetes.io/control-plane' \
    --output='jsonpath={.items[0].metadata.name}'
)

control_plane_ip=$(
  DOCKER_CONTEXT=default \
  docker inspect "$control_plane" \
    --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
)

printf 'Current control-plane IP: %s\n' "$control_plane_ip"
```

Back up the ConfigMap:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get configmap kube-proxy \
  --namespace kube-system \
  --output=yaml > /tmp/kube-proxy-before-ip-repair.yaml

cp /tmp/kube-proxy-before-ip-repair.yaml \
  /tmp/kube-proxy-ip-repair.yaml
```

On macOS, replace only the embedded API endpoint in the repair copy:

```bash
sed -i '' -E \
  "s#server: https://[^:]+:6443#server: https://${control_plane_ip}:6443#" \
  /tmp/kube-proxy-ip-repair.yaml
```

Review the result before applying it:

```bash
grep 'server:' /tmp/kube-proxy-ip-repair.yaml
```

Apply the corrected ConfigMap and restart the DaemonSet:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl apply -f /tmp/kube-proxy-ip-repair.yaml

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl rollout restart daemonset/kube-proxy \
  --namespace kube-system

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl rollout status daemonset/kube-proxy \
  --namespace kube-system \
  --timeout=2m
```

Confirm the new Pods loaded the current address and synced their caches:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods \
  --namespace kube-system \
  --selector=k8s-app=kube-proxy \
  --output=wide

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl logs \
  --namespace kube-system \
  --selector=k8s-app=kube-proxy \
  --since=5m \
  --prefix
```

Healthy logs contain `Caches are synced` and no requests to the old address.

Trigger Flux after Service routing is restored:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux reconcile kustomization flux-system \
  --with-source
```

## Step 10: Watch workload recovery

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

## Step 11: Unseal Vault

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

## Step 12: Verify RabbitMQ

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods,pvc --namespace rabbitmq

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace rabbitmq rabbitmq-0 -- \
  rabbitmq-diagnostics cluster_status
```

Confirm three running members, no alarms, and no network partitions.

## Step 13: Verify Flux

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

If kube-proxy fails after its edit, restore its ConfigMap and restart the
DaemonSet:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl apply -f /tmp/kube-proxy-before-ip-repair.yaml

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl rollout restart daemonset/kube-proxy \
  --namespace kube-system
```

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
- [ ] kube-proxy uses the current control-plane endpoint.
- [ ] kube-proxy logs show synced caches and no stale-IP TLS errors.
- [ ] ClusterIP Services are reachable.
- [ ] Flux controllers are Running.
- [ ] Vault is unsealed without reinitialization.
- [ ] RabbitMQ lists three running members.
- [ ] Flux Kustomizations and HelmReleases are Ready.
