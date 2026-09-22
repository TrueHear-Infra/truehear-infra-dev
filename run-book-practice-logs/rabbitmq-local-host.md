# Local-host RabbitMQ runbook

This runbook explains how RabbitMQ is deployed in the KROPS local workload
cluster and how its backend credentials are stored in Vault.

## Table of contents

- [Goal](#goal)
- [Architecture](#architecture)
- [Security rules](#security-rules)
- [Repository files](#repository-files)
- [Step 1: Verify prerequisites](#step-1-verify-prerequisites)
- [Step 2: Verify encrypted Secrets](#step-2-verify-encrypted-secrets)
- [Step 3: Validate the configuration](#step-3-validate-the-configuration)
- [Step 4: Publish the Helm chart](#step-4-publish-the-helm-chart)
- [Step 5: Publish the workload artifact](#step-5-publish-the-workload-artifact)
- [Step 6: Watch Flux](#step-6-watch-flux)
- [Step 7: Verify Kubernetes resources](#step-7-verify-kubernetes-resources)
- [Step 8: Verify RabbitMQ membership](#step-8-verify-rabbitmq-membership)
- [Step 9: Create the backend account](#step-9-create-the-backend-account)
- [Step 10: Patch the credentials into Vault](#step-10-patch-the-credentials-into-vault)
- [Step 11: Verify local TLS](#step-11-verify-local-tls)
- [Day-2 workflow](#day-2-workflow)
- [Restart behavior](#restart-behavior)
- [Troubleshooting](#troubleshooting)
- [Completion checklist](#completion-checklist)

## Goal

The local deployment provides:

- Three RabbitMQ members in one logical cluster.
- TLS-only AMQP on port `5671`.
- TLS-only management API on port `15671`.
- TLS-protected inter-node traffic on port `25672`.
- One persistent volume per member.
- A dedicated backend account on the `truehear` virtual host.
- Backend credentials stored in Vault instead of Git.

All three Pods can run on the single local CAPD worker. This verifies
application-level clustering, not host-level high availability. Cloud members
should run on separate workers and failure domains.

## Architecture

```text
Custom chart
    |
    | rabbitmq-chart-push
    v
Local OCI chart registry

Git-managed YAML
    |
    | oci-push
    v
Workload Flux
    |
    +--> decrypts SOPS Secrets
    +--> installs the HelmRelease
              |
              +--> rabbitmq-0 + PVC
              +--> rabbitmq-1 + PVC
              +--> rabbitmq-2 + PVC
```

## Security rules

- Never commit plaintext passwords, private TLS keys, or Erlang cookies.
- Commit only SOPS-encrypted `*.sops.yaml` Secret manifests.
- Keep `age.agekey` outside Git.
- Workload Flux reads the age private key from `flux-system/sops-age`.
- Use the bootstrap account only for administration.
- Use the separate `truehear-backend` account for the backend.
- Store backend credentials in Vault at `secret/app/truehear`.
- Never print decrypted Secrets, Vault tokens, or unseal shares.

## Repository files

```text
workload/local-host/rabbitmq/
├── chart/
├── namespace.yaml
├── serviceaccount.yaml
├── rabbitmq-tls.sops.yaml
├── rabbitmq-erlang-cookie.sops.yaml
├── rabbitmq-bootstrap-auth.sops.yaml
├── chart-source.yaml
├── helmrelease.yaml
├── kustomization.yaml
└── flux-ks.yaml
```

| File                 | Purpose                                          |
| -------------------- | ------------------------------------------------ |
| `chart/`             | Custom RabbitMQ chart.                           |
| `chart-source.yaml`  | Selects OCI chart version `0.1.0`.               |
| `helmrelease.yaml`   | Sets replicas, vhost, storage, and scheduling.   |
| `*.sops.yaml`        | Holds encrypted TLS, cookie, and bootstrap data. |
| `kustomization.yaml` | Lists the component resources.                   |
| `flux-ks.yaml`       | Makes Flux reconcile and decrypt the component.  |

The workload root registers `rabbitmq/flux-ks.yaml` in
`workload/local-host/kustomization.yaml`. The chart publication task is in
`mise.local-host.toml`.

## Step 1: Verify prerequisites

Check the workload nodes:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get nodes
```

Both nodes must be `Ready`.

Check workload Flux:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods --namespace flux-system
```

Check the default local StorageClass:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get storageclass
```

Expected provisioner:

```text
standard   rancher.io/local-path
```

Check the local registry:

```bash
DOCKER_CONTEXT=default \
docker ps --filter name=krops-registry
```

## Step 2: Verify encrypted Secrets

RabbitMQ needs these encrypted Secrets:

| Secret                    | Required keys                  |
| ------------------------- | ------------------------------ |
| `rabbitmq-tls`            | `tls.crt`, `tls.key`, `ca.crt` |
| `rabbitmq-erlang-cookie`  | `erlang-cookie`                |
| `rabbitmq-bootstrap-auth` | `username`, `password`         |

The TLS certificate must cover:

```text
rabbitmq.rabbitmq.svc.cluster.local
rabbitmq-0.rabbitmq-headless.rabbitmq.svc.cluster.local
rabbitmq-1.rabbitmq-headless.rabbitmq.svc.cluster.local
rabbitmq-2.rabbitmq-headless.rabbitmq.svc.cluster.local
```

Verify encryption without decrypting values:

```bash
for secret_file in \
  workload/local-host/rabbitmq/rabbitmq-tls.sops.yaml \
  workload/local-host/rabbitmq/rabbitmq-erlang-cookie.sops.yaml \
  workload/local-host/rabbitmq/rabbitmq-bootstrap-auth.sops.yaml
do
  printf '%s: ' "$secret_file"
  mise exec -- sops filestatus "$secret_file"
done
```

Each file must return:

```text
{"encrypted":true}
```

### Create or rotate the TLS Secret

Skip this procedure when the existing encrypted Secret is still valid.

Set paths to the existing PKI generation:

```bash
tls_certificate="/absolute/path/to/certs/tls.crt.pem"
tls_ca_chain="/absolute/path/to/certs/ca-chain.pem"
encrypted_tls_key="/absolute/path/to/private/tls.key.pem"
tls_manifest="workload/local-host/rabbitmq/rabbitmq-tls.sops.yaml"
```

Decrypt the private key only inside a temporary directory, create the Secret,
and immediately encrypt the manifest:

```bash
(
  set -e
  umask 077

  temporary_directory=$(mktemp -d)
  encryption_complete=0

  cleanup() {
    rm -rf "$temporary_directory"
    if [ "$encryption_complete" -ne 1 ]; then
      rm -f "$tls_manifest"
    fi
  }
  trap cleanup EXIT

  openssl pkey \
    -in "$encrypted_tls_key" \
    -out "$temporary_directory/tls.key"

  kubectl create secret generic rabbitmq-tls \
    --namespace rabbitmq \
    --from-file=tls.crt="$tls_certificate" \
    --from-file=tls.key="$temporary_directory/tls.key" \
    --from-file=ca.crt="$tls_ca_chain" \
    --dry-run=client \
    --output=yaml >"$tls_manifest"

  mise exec -- sops --encrypt --in-place \
    --input-type=yaml \
    --output-type=yaml \
    "$tls_manifest"

  encryption_complete=1
  chmod 644 "$tls_manifest"
  mise exec -- sops filestatus "$tls_manifest"
)
```

### Create the Erlang cookie Secret

Run this only for a new RabbitMQ cluster. Replacing the cookie on an existing
cluster requires a coordinated rotation.

```bash
(
  set -e
  umask 077

  cookie_manifest="workload/local-host/rabbitmq/rabbitmq-erlang-cookie.sops.yaml"
  encryption_complete=0

  cleanup() {
    if [ "$encryption_complete" -ne 1 ]; then
      rm -f "$cookie_manifest"
    fi
  }
  trap cleanup EXIT

  openssl rand -hex 32 |
    kubectl create secret generic rabbitmq-erlang-cookie \
      --namespace rabbitmq \
      --from-file=erlang-cookie=/dev/stdin \
      --dry-run=client \
      --output=yaml >"$cookie_manifest"

  mise exec -- sops --encrypt --in-place \
    --input-type=yaml \
    --output-type=yaml \
    "$cookie_manifest"

  encryption_complete=1
  chmod 644 "$cookie_manifest"
  mise exec -- sops filestatus "$cookie_manifest"
)
```

### Create the bootstrap credential Secret

These credentials seed fresh RabbitMQ PVCs. They are not the backend account.

```zsh
(
  set -e
  umask 077

  manifest="workload/local-host/rabbitmq/rabbitmq-bootstrap-auth.sops.yaml"
  encryption_complete=0

  cleanup() {
    unset rabbitmq_user rabbitmq_password rabbitmq_password_again
    if [ "$encryption_complete" -ne 1 ]; then
      rm -f "$manifest"
    fi
  }
  trap cleanup EXIT

  printf "RabbitMQ bootstrap username: " >&2
  IFS= read -r rabbitmq_user </dev/tty
  printf "RabbitMQ bootstrap password: " >&2
  IFS= read -rs rabbitmq_password </dev/tty
  printf "\nRepeat password: " >&2
  IFS= read -rs rabbitmq_password_again </dev/tty
  printf "\n" >&2

  if [ -z "$rabbitmq_user" ] ||
    [ -z "$rabbitmq_password" ] ||
    [ "$rabbitmq_password" != "$rabbitmq_password_again" ]; then
    echo "Username is empty or passwords do not match." >&2
    exit 1
  fi

  printf "%s" "$rabbitmq_password" |
    kubectl create secret generic rabbitmq-bootstrap-auth \
      --namespace rabbitmq \
      --from-literal="username=$rabbitmq_user" \
      --from-file=password=/dev/stdin \
      --dry-run=client \
      --output=yaml >"$manifest"

  unset rabbitmq_password rabbitmq_password_again

  mise exec -- sops --encrypt --in-place \
    --input-type=yaml \
    --output-type=yaml \
    "$manifest"

  encryption_complete=1
  chmod 644 "$manifest"
  mise exec -- sops filestatus "$manifest"
)
```

Verify that workload Flux has its age key without printing it:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get secret sops-age --namespace flux-system
```

Never apply a plaintext Secret directly. The commands above remove an
incomplete manifest if SOPS encryption fails.

## Step 3: Validate the configuration

```bash
kubectl kustomize workload/local-host/rabbitmq >/dev/null
kubectl kustomize workload/local-host >/dev/null
git diff --check -- workload/local-host
```

Lint the chart with local values:

```bash
helm lint workload/local-host/rabbitmq/chart --strict \
  --set fullnameOverride=rabbitmq \
  --set replicaCount=3 \
  --set vhost=truehear \
  --set persistence.storageClassName=standard \
  --set persistence.size=1Gi \
  --set oidc.enabled=false \
  --set scheduling.requireDistinctNodes=false
```

Before pushing the branch, run:

```bash
mise run validate
```

Do not describe a partial validation as green. Record unrelated failures.

## Step 4: Publish the Helm chart

Publish the chart before configuration that references it:

```bash
mise -E local-host run rabbitmq-chart-push
```

Expected result:

```text
Pushed: localhost:5001/charts/truehear-rabbitmq:0.1.0
```

This publishes the chart package only. It does not deploy RabbitMQ.

## Step 5: Publish the workload artifact

```bash
mise -E local-host run oci-push
```

The local deployment path is:

```text
Git checkout -> local OCI artifact -> workload Flux -> RabbitMQ
```

## Step 6: Watch Flux

Watch the Kustomization:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux get kustomizations --watch
```

Wait for `rabbitmq` to report `READY=True`, then press Ctrl-C.

Watch the HelmRelease:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux get helmreleases --all-namespaces --watch
```

The verified release used chart revision `0.1.0+57d99d1399aa`.

The `--watch` option never exits by itself. Press Ctrl-C after the required
resource is ready.

## Step 7: Verify Kubernetes resources

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods,pvc,svc --namespace rabbitmq --output=wide
```

Expected state:

- `rabbitmq-0`, `rabbitmq-1`, and `rabbitmq-2` are `1/1 Running`.
- Three `data-rabbitmq-*` PVCs are Bound.
- The client Service exposes ports `5671` and `15671`.
- The headless Service also exposes ports `4369` and `25672`.

## Step 8: Verify RabbitMQ membership

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace rabbitmq rabbitmq-0 -- \
  rabbitmq-diagnostics cluster_status
```

Verify:

- Three disk nodes.
- Three running nodes.
- No alarms.
- No network partitions.
- TLS listeners on `5671`, `15671`, and `25672`.

## Step 9: Create the backend account

Do not give the backend the bootstrap administrator account.

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec -it --namespace rabbitmq rabbitmq-0 -- \
  bash -lc '
    set -e

    read -rsp "Backend RabbitMQ password: " rabbitmq_password
    printf "\n"

    rabbitmqctl add_user truehear-backend "$rabbitmq_password"
    unset rabbitmq_password

    rabbitmqctl set_permissions \
      --vhost truehear \
      truehear-backend \
      ".*" \
      ".*" \
      ".*"
  '
```

Verify permissions:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace rabbitmq rabbitmq-0 -- \
  rabbitmqctl list_user_permissions truehear-backend
```

Expected result:

```text
vhost      configure  write  read
truehear   .*         .*     .*
```

## Step 10: Patch the credentials into Vault

Use `vault kv patch`, not `vault kv put`, so existing fields remain at
`secret/app/truehear`.

```zsh
(
  set -e

  printf "Vault administrative token: " >&2
  IFS= read -rs vault_admin_token </dev/tty
  printf "\nRabbitMQ backend password: " >&2
  IFS= read -rs rabbitmq_password </dev/tty
  printf "\n" >&2

  printf "%s" "$rabbitmq_password" |
    KUBECONFIG="$PWD/local-workload.kubeconfig" \
    kubectl exec -i --namespace vault vault-0 -- \
      env \
        VAULT_ADDR=https://127.0.0.1:8200 \
        VAULT_SKIP_VERIFY=true \
        VAULT_TOKEN="$vault_admin_token" \
      vault kv patch \
        -mount=secret \
        -method=patch \
        app/truehear \
        RABBITMQ_USERNAME=truehear-backend \
        RABBITMQ_PASSWORD=-

  unset vault_admin_token rabbitmq_password
)
```

Only `RABBITMQ_USERNAME` and `RABBITMQ_PASSWORD` are added or updated.

## Step 11: Verify local TLS

Start a port-forward in one terminal:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl port-forward --namespace rabbitmq service/rabbitmq 5671:5671
```

Verify the certificate from another terminal:

```bash
openssl s_client \
  -connect 127.0.0.1:5671 \
  -servername rabbitmq.rabbitmq.svc.cluster.local \
  -CAfile /absolute/path/to/certs/ca-chain.pem \
  -verify_return_error </dev/null
```

Stop the port-forward with Ctrl-C. A backend running in Kubernetes uses:

```text
rabbitmq.rabbitmq.svc.cluster.local:5671
```

## Day-2 workflow

For a chart template or chart default change:

1. Bump `chart/Chart.yaml`.
2. Update the tag in `chart-source.yaml`.
3. Validate the chart and Kustomize roots.
4. Run `rabbitmq-chart-push`.
5. Run `oci-push`.
6. Watch Flux and verify membership.

For only a HelmRelease value or manifest change:

1. Edit repository YAML.
2. Validate the Kustomize roots.
3. Run `oci-push`.
4. Watch Flux reconcile.

Never use direct `kubectl edit` for persistent changes.

## Restart behavior

- RabbitMQ data survives Pod recreation because every member has a PVC.
- The shared Erlang cookie must remain unchanged.
- Members normally rejoin automatically after worker recovery.
- The local profile has one worker, so one worker failure affects all members.
- Vault must be unsealed before the backend can read RabbitMQ credentials.

## Troubleshooting

### RabbitMQ does not appear in Flux

Confirm `rabbitmq/flux-ks.yaml` is registered in
`workload/local-host/kustomization.yaml`, then run `oci-push`.

### Chart source is not ready

Run `rabbitmq-chart-push` before `oci-push`, then inspect:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux get sources oci --all-namespaces
```

### Pods remain Pending

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" kubectl get nodes
KUBECONFIG="$PWD/local-workload.kubeconfig" kubectl get pvc -n rabbitmq
```

The local HelmRelease sets `scheduling.requireDistinctNodes=false` because the
CAPD workload cluster has one worker.

### All workload applications start terminating

Check for a `NotReady` worker and follow [Docker IP recovery](recovery.md).
Do not delete PVCs.

## Completion checklist

- [ ] Both workload nodes are Ready.
- [ ] All SOPS manifests report `encrypted:true`.
- [ ] The chart exists in the local OCI registry.
- [ ] The RabbitMQ Kustomization and HelmRelease are Ready.
- [ ] All three Pods are `1/1 Running`.
- [ ] All three PVCs are Bound.
- [ ] Cluster status lists three running nodes.
- [ ] There are no alarms or network partitions.
- [ ] The backend account has permissions on only the `truehear` vhost.
- [ ] Backend credentials were patched into Vault.
