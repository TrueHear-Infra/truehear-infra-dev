# Local-host Redis Cluster runbook

This runbook explains how to deploy, initialize, verify, and locally access the
six-node Redis Cluster in the KROPS workload cluster.

## Table of contents

- [Goal](#goal)
- [Architecture](#architecture)
- [Security rules](#security-rules)
- [Repository files](#repository-files)
- [Step 1: Verify prerequisites](#step-1-verify-prerequisites)
- [Step 2: Select the current Redis certificate](#step-2-select-the-current-redis-certificate)
- [Step 3: Create the encrypted TLS Secret](#step-3-create-the-encrypted-tls-secret)
- [Step 4: Prepare Redis authentication](#step-4-prepare-redis-authentication)
- [Step 5: Validate the configuration](#step-5-validate-the-configuration)
- [Step 6: Publish the Redis chart](#step-6-publish-the-redis-chart)
- [Step 7: Publish the workload artifact](#step-7-publish-the-workload-artifact)
- [Step 8: Watch Flux](#step-8-watch-flux)
- [Step 9: Verify Pods and storage](#step-9-verify-pods-and-storage)
- [Step 10: Initialize the Redis Cluster](#step-10-initialize-the-redis-cluster)
- [Step 11: Verify cluster health](#step-11-verify-cluster-health)
- [Step 12: Store backend credentials in Vault](#step-12-store-backend-credentials-in-vault)
- [Step 13: Prepare Mac routing](#step-13-prepare-mac-routing)
- [Step 14: Open local tunnels](#step-14-open-local-tunnels)
- [Step 15: Generate the backend JWT](#step-15-generate-the-backend-jwt)
- [Step 16: Test through the backend](#step-16-test-through-the-backend)
- [Day-2 workflow](#day-2-workflow)
- [Troubleshooting](#troubleshooting)
- [Completion checklist](#completion-checklist)

## Goal

The local deployment provides:

- Six Redis processes in one Redis Cluster.
- Three primaries and three replicas.
- All 16,384 Redis hash slots.
- TLS on application, replication, and cluster-bus connections.
- One persistent volume per Redis Pod.
- A restricted `truehear-backend-dev` ACL user.
- Backend credentials stored in Vault instead of Git.

All six Pods run on the single local CAPD worker. This tests Redis clustering
and the application connection flow. It does not provide host-level high
availability.

## Architecture

```text
Custom Redis chart
        |
        | redis-chart-push
        v
Local OCI chart registry

Git-managed Redis YAML and SOPS Secrets
        |
        | oci-push
        v
Workload Flux
        |
        +--> decrypts the Redis Secrets
        +--> installs the Redis HelmRelease
                  |
                  +--> redis-0, redis-1, redis-2
                  +--> redis-3, redis-4, redis-5
                  +--> six persistent volumes
```

The chart starts six independent cluster-enabled Redis servers. The one-time
`redis-cli --cluster create` operation assigns hash slots and joins them into
one logical Cluster.

## Security rules

- Never commit Redis passwords or plaintext private keys.
- Commit only SOPS-encrypted `*.sops.yaml` Secret manifests.
- Keep `age.agekey` gitignored.
- Use the default Redis password only for administration and bootstrap.
- Give the backend only the restricted `truehear-backend-dev` ACL identity.
- Store the backend username and password at `secret/app/truehear` in Vault.
- Never reset or recreate an existing Redis Cluster without checking its data.
- The Redis cluster-bus requires certificates that support both server and
  client authentication.

## Repository files

```text
workload/local-host/redis/
├── chart/
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
├── namespace.yaml
├── serviceaccount.yaml
├── redis-tls.sops.yaml
├── redis-auth.sops.yaml
├── redis-backend-acl.sops.yaml
├── chart-source.yaml
├── helmrelease.yaml
├── kustomization.yaml
└── flux-ks.yaml
```

| File                          | Purpose                         |
| ----------------------------- | ------------------------------- |
| `chart/`                      | TrueHear-owned Helm chart.      |
| `chart-source.yaml`           | Selects chart version `0.3.0`.  |
| `helmrelease.yaml`            | Sets nodes, storage, and ACLs.  |
| `redis-tls.sops.yaml`         | Encrypted TLS material.         |
| `redis-auth.sops.yaml`        | Encrypted default auth.         |
| `redis-backend-acl.sops.yaml` | Encrypted backend ACL.          |
| `kustomization.yaml`          | Lists component resources.      |
| `flux-ks.yaml`                | Configures Flux reconciliation. |

The workload root registers `redis/flux-ks.yaml` in
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

Check workload storage:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get storageclass
```

The local workload expects the default `standard` StorageClass using
`rancher.io/local-path`.

Check the local OCI registry:

```bash
DOCKER_CONTEXT=default \
docker ps --filter name=krops-registry
```

## Step 2: Select the current Redis certificate

The Redis PKI leaf directory may contain multiple generations. Do not infer
the current generation from the leaf ID or choose an older directory by name.
Read `current.json`.

```bash
redis_leaf="/Users/sameer/sameer/cloud-infra-development-pulumi/secure/pkiv2-pki/authorities/root-11338dd724/intermediates/intermediate-11338dd724-ea67e40de3/issued/node-11338dd724-ea67e40de3-6efe1459d8"

jq . "$redis_leaf/current.json"
```

During this exercise, the current generation was:

```text
generation-026d6fd7e6
```

Verify its extended key usages:

```bash
redis_generation="$redis_leaf/generations/generation-026d6fd7e6"

openssl x509 \
  -in "$redis_generation/certs/tls.crt.pem" \
  -noout \
  -serial \
  -fingerprint \
  -sha256 \
  -ext extendedKeyUsage
```

The certificate must contain both:

```text
TLS Web Server Authentication
TLS Web Client Authentication
```

It must also cover all six Pod DNS names:

```text
redis-0.redis-headless.redis.svc.cluster.local
redis-1.redis-headless.redis.svc.cluster.local
redis-2.redis-headless.redis.svc.cluster.local
redis-3.redis-headless.redis.svc.cluster.local
redis-4.redis-headless.redis.svc.cluster.local
redis-5.redis-headless.redis.svc.cluster.local
```

`tls-auth-clients no` disables client certificates for application clients.
It does not disable certificate authentication on the Redis cluster-bus.

## Step 3: Create the encrypted TLS Secret

Set the paths to the generation selected through `current.json`:

```bash
tls_certificate="$redis_generation/certs/tls.crt.pem"
tls_ca_chain="$redis_generation/certs/ca-chain.pem"
encrypted_tls_key="$redis_generation/private/tls.key.pem"
tls_manifest="workload/local-host/redis/redis-tls.sops.yaml"
```

Create and encrypt the manifest safely. The existing repository file is
replaced only after certificate-key verification and SOPS encryption succeed.

```bash
(
  set -euo pipefail
  umask 077

  temporary_directory=$(mktemp -d)
  trap 'rm -rf "$temporary_directory"' EXIT

  openssl pkey \
    -in "$encrypted_tls_key" \
    -out "$temporary_directory/tls.key"

  certificate_public_key=$(
    openssl x509 -in "$tls_certificate" -pubkey -noout |
      openssl pkey -pubin -outform DER 2>/dev/null |
      shasum -a 256
  )

  private_public_key=$(
    openssl pkey -in "$temporary_directory/tls.key" -pubout -outform DER |
      shasum -a 256
  )

  if [ "$certificate_public_key" != "$private_public_key" ]; then
    echo "ERROR: Redis certificate and private key do not match" >&2
    exit 1
  fi

  kubectl create secret generic redis-tls \
    --namespace redis \
    --from-file=tls.crt="$tls_certificate" \
    --from-file=tls.key="$temporary_directory/tls.key" \
    --from-file=ca.crt="$tls_ca_chain" \
    --dry-run=client \
    --output=yaml >"$temporary_directory/redis-tls.sops.yaml"

  mise exec -- sops --encrypt \
    --filename-override "$tls_manifest" \
    --input-type=yaml \
    --output-type=yaml \
    "$temporary_directory/redis-tls.sops.yaml" \
    >"$temporary_directory/redis-tls.encrypted.yaml"

  mise exec -- sops filestatus \
    "$temporary_directory/redis-tls.encrypted.yaml"

  mv "$temporary_directory/redis-tls.encrypted.yaml" "$tls_manifest"
  chmod 644 "$tls_manifest"
)
```

Verify only the public certificate stored inside the encrypted manifest:

```bash
SOPS_AGE_KEY_FILE="$PWD/age.agekey" \
mise exec -- sops --decrypt \
  --extract '["data"]["tls.crt"]' \
  workload/local-host/redis/redis-tls.sops.yaml |
  base64 --decode |
  openssl x509 \
    -noout \
    -serial \
    -fingerprint \
    -sha256 \
    -ext extendedKeyUsage
```

This command does not print the private key.

## Step 4: Prepare Redis authentication

Redis uses two encrypted authentication manifests:

| Secret              | Key               | Purpose                 |
| ------------------- | ----------------- | ----------------------- |
| `redis-auth`        | `redis-auth.conf` | Admin and replica auth. |
| `redis-backend-acl` | `users.conf`      | Restricted backend ACL. |

The default authentication file has this structure:

```text
requirepass REPLACE_WITH_RANDOM_PASSWORD
masterauth REPLACE_WITH_THE_SAME_PASSWORD
```

Create its encrypted Secret without writing the password to the repository:

```zsh
read -s "redis_admin_password?Redis administrative password: "
echo
test -n "$redis_admin_password"

(
  set -euo pipefail
  umask 077

  temporary_directory=$(mktemp -d)
  trap 'rm -rf "$temporary_directory"' EXIT

  printf 'requirepass %s\nmasterauth %s\n' \
    "$redis_admin_password" \
    "$redis_admin_password" \
    >"$temporary_directory/redis-auth.conf"

  kubectl create secret generic redis-auth \
    --namespace redis \
    --from-file=redis-auth.conf="$temporary_directory/redis-auth.conf" \
    --dry-run=client \
    --output=yaml >"$temporary_directory/redis-auth.yaml"

  mise exec -- sops --encrypt \
    --filename-override workload/local-host/redis/redis-auth.sops.yaml \
    --input-type=yaml \
    --output-type=yaml \
    "$temporary_directory/redis-auth.yaml" \
    >"$temporary_directory/redis-auth.encrypted.yaml"

  mv "$temporary_directory/redis-auth.encrypted.yaml" \
    workload/local-host/redis/redis-auth.sops.yaml
  chmod 644 workload/local-host/redis/redis-auth.sops.yaml
)

unset redis_admin_password
```

The backend ACL username used locally is:

```text
truehear-backend-dev
```

The ACL grants only the commands required by the backend and stores a SHA-256
password hash, not the plaintext backend password:

```text
~* +auth +hello +ping +client|setinfo +client|setname +cluster|slots
+cluster|shards +asking +get +set +setex +del +expire +pexpire +incr
+scan +decr +pttl +script|load +evalsha
```

Create the backend ACL Secret. Save the chosen password in your password
manager because the same plaintext value is stored in Vault in Step 12.

```zsh
read -s "redis_backend_password?Redis backend password: "
echo
test -n "$redis_backend_password"

(
  set -euo pipefail
  umask 077

  temporary_directory=$(mktemp -d)
  trap 'rm -rf "$temporary_directory"' EXIT

  password_hash=$(
    printf '%s' "$redis_backend_password" |
      shasum -a 256 |
      awk '{print $1}'
  )

  printf '%s\n' \
    "user truehear-backend-dev on #${password_hash} ~* +auth +hello +ping +client|setinfo +client|setname +cluster|slots +cluster|shards +asking +get +set +setex +del +expire +pexpire +incr +scan +decr +pttl +script|load +evalsha" \
    >"$temporary_directory/users.conf"

  kubectl create secret generic redis-backend-acl \
    --namespace redis \
    --from-file=users.conf="$temporary_directory/users.conf" \
    --dry-run=client \
    --output=yaml >"$temporary_directory/redis-backend-acl.yaml"

  mise exec -- sops --encrypt \
    --filename-override workload/local-host/redis/redis-backend-acl.sops.yaml \
    --input-type=yaml \
    --output-type=yaml \
    "$temporary_directory/redis-backend-acl.yaml" \
    >"$temporary_directory/redis-backend-acl.encrypted.yaml"

  mv "$temporary_directory/redis-backend-acl.encrypted.yaml" \
    workload/local-host/redis/redis-backend-acl.sops.yaml
  chmod 644 workload/local-host/redis/redis-backend-acl.sops.yaml
)

unset redis_backend_password
```

Keep the plaintext backend password only long enough to place it in Vault.
Verify all three manifests remain encrypted:

```bash
for secret_file in \
  workload/local-host/redis/redis-tls.sops.yaml \
  workload/local-host/redis/redis-auth.sops.yaml \
  workload/local-host/redis/redis-backend-acl.sops.yaml
do
  printf '%s: ' "$secret_file"
  mise exec -- sops filestatus "$secret_file"
done
```

Each file must return:

```text
{"encrypted":true}
```

## Step 5: Validate the configuration

Run the repository validation before publishing:

```bash
mise run validate
```

If an unrelated optional Python test reports a missing local module, resolve
that development dependency separately. Do not treat it as proof that the
Redis manifests are invalid.

Build the Redis component directly:

```bash
mise exec -- kubectl kustomize workload/local-host/redis >/dev/null
```

Build the complete local workload:

```bash
mise exec -- kubectl kustomize workload/local-host >/dev/null
```

## Step 6: Publish the Redis chart

The workload artifact contains the HelmRelease and OCIRepository objects. The
custom Helm chart is a separate OCI artifact and must be pushed first.

```bash
mise -E local-host run redis-chart-push
```

Expected chart:

```text
truehear-redis-cluster:0.3.0
```

## Step 7: Publish the workload artifact

Publish the current management and workload configuration:

```bash
mise -E local-host run oci-push
```

This is the local equivalent of merging Git-managed configuration into the
branch watched by Flux.

## Step 8: Watch Flux

Watch the Redis Kustomization:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux get kustomizations --watch
```

Stop the watch with Ctrl-C after `redis` reports `READY=True`.

Check the HelmRelease:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux get helmreleases --all-namespaces
```

If a new OCI revision does not reconcile immediately:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux reconcile kustomization redis --with-source
```

## Step 9: Verify Pods and storage

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods,pvc,svc --namespace redis --output=wide
```

Expected state:

- Six `redis-N` Pods are `1/1 Running`.
- Six `data-redis-N` PVCs are `Bound`.
- `redis-headless` exposes TLS port `6379` and cluster-bus port `16379`.

At this point, the processes are running but may still be six independent,
unformed Redis nodes.

## Step 10: Initialize the Redis Cluster

Check the initial state without printing the administrative password:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace redis redis-0 -- sh -ec '
  IFS=" " read -r directive REDISCLI_AUTH \
    < /etc/redis/auth/redis-auth.conf
  test "$directive" = "requirepass"
  export REDISCLI_AUTH

  redis-cli -e --tls \
    --cacert /etc/redis/tls/ca.crt \
    CLUSTER INFO
'
```

For a new cluster, `cluster_slots_assigned:0` and `cluster_known_nodes:1` are
expected.

Create the cluster exactly once:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec -it --namespace redis redis-0 -- sh -ec '
  IFS=" " read -r directive REDISCLI_AUTH \
    < /etc/redis/auth/redis-auth.conf
  test "$directive" = "requirepass"
  test -n "$REDISCLI_AUTH"
  export REDISCLI_AUTH

  redis-cli -e --tls \
    --cacert /etc/redis/tls/ca.crt \
    --cluster create \
    redis-0.redis-headless.redis.svc.cluster.local:6379 \
    redis-1.redis-headless.redis.svc.cluster.local:6379 \
    redis-2.redis-headless.redis.svc.cluster.local:6379 \
    redis-3.redis-headless.redis.svc.cluster.local:6379 \
    redis-4.redis-headless.redis.svc.cluster.local:6379 \
    redis-5.redis-headless.redis.svc.cluster.local:6379 \
    --cluster-replicas 1 \
    --cluster-yes
'
```

Successful output ends with:

```text
[OK] All 16384 slots covered.
```

Do not run cluster creation again after it succeeds. Membership and slot data
are persisted in each Pod's `nodes.conf` on its PVC.

## Step 11: Verify cluster health

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace redis redis-0 -- sh -ec '
  IFS=" " read -r directive REDISCLI_AUTH \
    < /etc/redis/auth/redis-auth.conf
  test "$directive" = "requirepass"
  export REDISCLI_AUTH

  redis-cli -e --tls \
    --cacert /etc/redis/tls/ca.crt \
    CLUSTER INFO

  redis-cli -e --tls \
    --cacert /etc/redis/tls/ca.crt \
    CLUSTER NODES
'
```

Required values:

```text
cluster_state:ok
cluster_slots_assigned:16384
cluster_slots_ok:16384
cluster_slots_fail:0
cluster_known_nodes:6
cluster_size:3
```

`CLUSTER NODES` must show three connected masters and three connected slaves.

## Step 12: Store backend credentials in Vault

Patch only the Redis keys so existing Vault values remain intact:

```zsh
read -s "vault_token?Vault token: "
echo
read -s "redis_password?Redis backend password: "
echo

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec -it --namespace vault vault-0 -- \
  env \
    VAULT_ADDR=https://127.0.0.1:8200 \
    VAULT_SKIP_VERIFY=true \
    VAULT_TOKEN="$vault_token" \
    vault kv patch secret/app/truehear \
      REDIS_USERNAME=truehear-backend-dev \
      "REDIS_PASSWORD=${redis_password}"

unset vault_token redis_password
```

`vault kv patch` preserves the RabbitMQ and other application values already
stored at the same path.

## Step 13: Prepare Mac routing

The Redis Cluster advertises all six Pod DNS names. A cluster-aware backend
connects to a seed, downloads the slot map, and then connects directly to the
advertised node names.

Use separate loopback addresses so every node can use local port `6379`:

```text
127.0.0.2 redis-0.redis-headless.redis.svc.cluster.local
127.0.0.3 redis-1.redis-headless.redis.svc.cluster.local
127.0.0.4 redis-2.redis-headless.redis.svc.cluster.local
127.0.0.5 redis-3.redis-headless.redis.svc.cluster.local
127.0.0.6 redis-4.redis-headless.redis.svc.cluster.local
127.0.0.7 redis-5.redis-headless.redis.svc.cluster.local
```

These entries belong in `/etc/hosts`. The matching loopback aliases must also
exist on `lo0`.

Verify both without changing the Mac:

```bash
grep 'redis-[0-5].redis-headless.redis.svc.cluster.local' /etc/hosts
ifconfig lo0 | grep 'inet 127.0.0.'
```

## Step 14: Open local tunnels

Open all six Redis tunnels in one terminal:

```zsh
forward_pids=()

cleanup_redis_tunnels() {
  for forward_pid in "${forward_pids[@]}"; do
    kill "$forward_pid" 2>/dev/null || true
  done
}

trap cleanup_redis_tunnels EXIT INT TERM

for node_number in {0..5}; do
  local_address="127.0.0.$((node_number + 2))"

  KUBECONFIG="$PWD/local-workload.kubeconfig" \
  kubectl port-forward \
    --namespace redis \
    --address "$local_address" \
    "pod/redis-${node_number}" \
    6379:6379 &

  forward_pids+=("$!")
done

wait
```

Keep the terminal open. Ctrl-C runs the cleanup function and stops every
background port-forward.

Open the Vault tunnel in a second terminal:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl port-forward \
  --namespace vault \
  pod/vault-0 \
  8200:8200
```

The backend needs Vault to retrieve its Redis username and password.

## Step 15: Generate the backend JWT

Generate a fresh 30-minute Kubernetes JWT at the path used by the backend:

```bash
umask 077

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl create token truehear-backend \
  --namespace backend \
  --audience vault \
  --duration 30m \
  > /tmp/truehear-vault-local.xRCKeU/backend.jwt

chmod 600 /tmp/truehear-vault-local.xRCKeU/backend.jwt
```

This JWT is short-lived. Generate another one after it expires.

## Step 16: Test through the backend

The backend Redis cluster settings are:

```dotenv
REDIS_MODE=cluster
REDIS_CLUSTER_ROOT_NODES=redis-0.redis-headless.redis.svc.cluster.local:6379,redis-1.redis-headless.redis.svc.cluster.local:6379,redis-2.redis-headless.redis.svc.cluster.local:6379
REDIS_DATABASE=0
REDIS_INTEGRATION=1
REDIS_TLS_ENABLED=1
REDIS_TLS_REJECT_UNAUTHORIZED=1
REDIS_TLS_SERVERNAME=
REDIS_TLS_CA_PATH=/absolute/path/to/the/current/ca-chain.pem
```

The backend reads `REDIS_USERNAME` and `REDIS_PASSWORD` from Vault. Do not put
them in `.env`.

From the backend repository, run its Redis Cluster smoke test:

```bash
npm run -s script -- \
  src/modules/shared/smoke-tests/cache/redis.cluster.smoke-test.ts |
  jq -R 'fromjson? // .'
```

Expected result:

```text
Vault credentials + Redis Cluster CacheService: OK
```

## Day-2 workflow

For YAML-only changes:

```text
Edit Git-managed YAML
        |
        v
mise run validate
        |
        v
mise -E local-host run oci-push
        |
        v
Flux reconciles Redis
```

For chart changes, publish the new chart before the configuration artifact:

```bash
mise -E local-host run redis-chart-push
mise -E local-host run oci-push
```

For a TLS rotation:

1. Read the Redis leaf's `current.json`.
2. Verify the selected certificate has `serverAuth` and `clientAuth`.
3. Rebuild `redis-tls.sops.yaml` safely.
4. Publish the workload artifact.
5. Confirm the live Secret's serial and fingerprint.
6. Restart the Redis StatefulSet so Redis loads the new files.
7. Verify cluster health after the rollout.

Never run `redis-cli --cluster create` during a normal rotation or restart.

## Troubleshooting

### Cluster creation waits indefinitely

Do not keep waiting after repeated dots. Stop the command with Ctrl-C and
inspect the cluster:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace redis redis-0 -- sh -ec '
  IFS=" " read -r directive REDISCLI_AUTH \
    < /etc/redis/auth/redis-auth.conf
  export REDISCLI_AUTH
  redis-cli -e --tls --cacert /etc/redis/tls/ca.crt CLUSTER INFO
  redis-cli -e --tls --cacert /etc/redis/tls/ca.crt CLUSTER NODES
'
```

A common cause is deploying an older server-only certificate. Check the live
public certificate:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get secret redis-tls \
  --namespace redis \
  --output='jsonpath={.data.tls\.crt}' |
  base64 --decode |
  openssl x509 \
    -noout \
    -serial \
    -fingerprint \
    -sha256 \
    -ext extendedKeyUsage
```

Compare it with the generation selected by the PKI leaf's `current.json`.

### Recover a failed first creation

Use this only for a new cluster whose creation failed before application data
was written. The command refuses to reset any node whose `DBSIZE` is not zero.

```zsh
for pod in redis-{0..5}; do
  echo "==> Resetting $pod"

  KUBECONFIG="$PWD/local-workload.kubeconfig" \
  kubectl exec --namespace redis "$pod" -- sh -ec '
    IFS=" " read -r directive REDISCLI_AUTH \
      < /etc/redis/auth/redis-auth.conf
    test "$directive" = "requirepass"
    test -n "$REDISCLI_AUTH"
    export REDISCLI_AUTH

    database_size=$(
      redis-cli -e --tls \
        --cacert /etc/redis/tls/ca.crt \
        -h 127.0.0.1 \
        -p 6379 \
        DBSIZE
    )

    test "$database_size" = "0" || {
      echo "ERROR: refusing to reset a non-empty Redis node" >&2
      exit 1
    }

    redis-cli -e --tls \
      --cacert /etc/redis/tls/ca.crt \
      -h 127.0.0.1 \
      -p 6379 \
      CLUSTER RESET HARD
  '
done
```

After all nodes return `OK`, rerun the one-time creation command.

### Redis logs contain unexpected TLS EOF errors

The chart currently uses TCP startup, readiness, and liveness probes against
the TLS-only application port. A raw TCP probe can produce an
`unexpected eof while reading` TLS log entry.

Do not diagnose cluster failure from that line alone. Use `CLUSTER INFO`,
`CLUSTER NODES`, and the cluster-bus certificate checks.

### SOPS cannot decrypt the manifest

Point SOPS at the local age private key:

```bash
export SOPS_AGE_KEY_FILE="$PWD/age.agekey"
```

The private age key stays outside Git. Flux uses its own copy from the
`flux-system/sops-age` Kubernetes Secret.

### Flux cannot download the OCI artifact

If Flux reports a timeout reaching `source-controller`, inspect kube-proxy.
This is workload Service networking, not a Redis manifest failure. Follow the
[local Docker IP recovery runbook](recovery.md#step-9-repair-kube-proxy).

### Backend reports Vault connection refused

This error occurs before Redis or RabbitMQ is tested:

```text
Vault Kubernetes login failed: connect ECONNREFUSED 127.0.0.1:8200
```

Start the Vault port-forward from Step 14 and retry.

### Background Redis tunnels do not stop

If a tunnel command was started without the cleanup trap, list the exact
processes:

```bash
pgrep -af 'kubectl port-forward.*redis'
```

Stop only the returned Redis port-forward process IDs with `kill`. Do not use a
broad process pattern when unrelated kubectl port-forwards are running.

## Completion checklist

- [ ] The current PKI generation was selected through `current.json`.
- [ ] The Redis certificate has server and client authentication EKUs.
- [ ] All three Redis Secret manifests are SOPS-encrypted.
- [ ] The Redis chart and workload artifact were published.
- [ ] Flux reports the Redis Kustomization and HelmRelease Ready.
- [ ] Six Redis Pods are Running and six PVCs are Bound.
- [ ] Redis reports six members and all 16,384 slots.
- [ ] Three primaries and three replicas are connected.
- [ ] The backend credentials are stored in Vault.
- [ ] All six Pod DNS names resolve to their local loopback addresses.
- [ ] The Vault and Redis port-forwards are running during the local test.
- [ ] The backend JWT is fresh and permission-restricted.
- [ ] The backend Redis Cluster smoke test succeeds.
