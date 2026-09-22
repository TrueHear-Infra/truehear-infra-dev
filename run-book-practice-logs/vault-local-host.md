# Local-host Vault and backend authentication runbook

This runbook records the complete local KROPS workflow for storage, Vault,
SOPS, Raft initialization, and backend authentication. It uses the
`local-host` profile and Docker-based CAPD clusters. It does not create AWS
resources.

The companion [local artifact rules](rules.md) explain which repository files
must be updated whenever a management or workload component is added.

## Table of contents

- [Goal](#goal)
- [Architecture](#architecture)
- [Important security boundaries](#important-security-boundaries)
- [Repository files](#repository-files)
- [Prerequisites](#prerequisites)
- [Step 1: Select the workload cluster](#step-1-select-the-workload-cluster)
- [Step 2: Verify workload storage](#step-2-verify-workload-storage)
- [Step 3: Prepare SOPS and age](#step-3-prepare-sops-and-age)
- [Step 4: Verify the encrypted Vault TLS Secret](#step-4-verify-the-encrypted-vault-tls-secret)
- [Step 5: Publish the Vault configuration](#step-5-publish-the-vault-configuration)
- [Step 6: Verify the Vault deployment](#step-6-verify-the-vault-deployment)
- [Step 7: Initialize Vault](#step-7-initialize-vault)
- [Step 8: Unseal all Vault members](#step-8-unseal-all-vault-members)
- [Step 9: Verify the Raft cluster](#step-9-verify-the-raft-cluster)
- [Step 10: Verify Vault can review Kubernetes JWTs](#step-10-verify-vault-can-review-kubernetes-jwts)
- [Step 11: Publish the backend identity](#step-11-publish-the-backend-identity)
- [Step 12: Enable Vault Kubernetes authentication](#step-12-enable-vault-kubernetes-authentication)
- [Step 13: Create the backend policy](#step-13-create-the-backend-policy)
- [Step 14: Create the backend Vault role](#step-14-create-the-backend-vault-role)
- [Step 15: Create a test secret](#step-15-create-a-test-secret)
- [Step 16: Test the complete identity chain](#step-16-test-the-complete-identity-chain)
- [Step 17: Test from a backend running on macOS](#step-17-test-from-a-backend-running-on-macos)
- [Day-2 update workflow](#day-2-update-workflow)
- [Restart and recovery behavior](#restart-and-recovery-behavior)
- [Troubleshooting](#troubleshooting)
- [Completion checklist](#completion-checklist)

## Goal

The goal is to prove this complete path locally:

```text
Git-managed local-host configuration
                ↓
Local OCI registry
                ↓
Management Flux
                ↓
Storage delivered to local-workload
                ↓
Workload Flux decrypts Vault TLS with SOPS
                ↓
Vault runs as a three-member Raft cluster
                ↓
Backend ServiceAccount JWT
                ↓
Vault Kubernetes authentication
                ↓
Restricted application secret read
```

Keycloak is not required for this flow. Keycloak authenticates human users to
applications. The backend uses its Kubernetes ServiceAccount identity to
authenticate to Vault.

## Architecture

```text
local-management cluster
│
├── Management Flux
├── CAPI and CAPD
└── ClusterResourceSet
    └── delivers local-path storage to local-workload

local-workload cluster
│
├── Workload Flux
├── local-path provisioner
├── Vault
│   ├── vault-0
│   ├── vault-1
│   └── vault-2
└── backend identity
    └── ServiceAccount: backend/truehear-backend
```

The management cluster carries the delivery objects for workload storage. The
actual StorageClass, provisioner Pod, Vault Pods, PVCs, and backend identity
exist in the workload cluster.

## Important security boundaries

- `age.agekey` is the SOPS private key. It decrypts Git-managed secrets.
- `vault-tls.sops.yaml` is safe to commit only while it remains encrypted.
- Vault unseal shares and the Vault root token are not Kubernetes Secrets and
  must never be committed.
- The SOPS age key cannot unseal Vault.
- A Kubernetes ServiceAccount JWT is not a Vault token. Vault verifies the JWT
  and returns a separate, short-lived Vault token.
- The backend role can read only `secret/data/app/truehear`.
- Do not use `curl -k`, `VAULT_SKIP_VERIFY`, or another TLS bypass.
- Do not commit kubeconfig files, generated JWTs, root tokens, or unseal keys.

## Repository files

<!-- markdownlint-disable MD013 -->

### Storage delivery

| File                                                         | Purpose                                       |
| ------------------------------------------------------------ | --------------------------------------------- |
| `mgmt/local-host/addons/storage/local-path-provisioner.yaml` | Workload StorageClass and provisioner payload |
| `mgmt/local-host/addons/storage/kustomization.yaml`          | Builds the storage payload                    |
| `mgmt/local-host/addons/storage/flux-ks.yaml`                | Reconciles storage after the workload CNI     |
| `mgmt/local-host/kustomization.yaml`                         | Registers the storage Flux Kustomization      |
| `mgmt/local-host/addons/flux-apps/flux-ks.yaml`              | Waits for storage before workload Flux        |

### Vault

| File                                            | Purpose                                               |
| ----------------------------------------------- | ----------------------------------------------------- |
| `workload/local-host/vault/namespace.yaml`      | Creates the `vault` namespace                         |
| `workload/local-host/vault/serviceaccount.yaml` | Creates the Vault workload identity                   |
| `workload/local-host/vault/vault-tls.sops.yaml` | Encrypted Vault certificate, key, and CA chain        |
| `workload/local-host/vault/helmrepository.yaml` | Declares the HashiCorp chart repository               |
| `workload/local-host/vault/helmrelease.yaml`    | Runs three Vault members with integrated Raft storage |
| `workload/local-host/vault/kustomization.yaml`  | Builds the Vault resources                            |
| `workload/local-host/vault/flux-ks.yaml`        | Enables SOPS decryption and reconciliation            |

### Backend identity

| File                                              | Purpose                                                           |
| ------------------------------------------------- | ----------------------------------------------------------------- |
| `workload/local-host/backend/namespace.yaml`      | Creates the `backend` namespace                                   |
| `workload/local-host/backend/serviceaccount.yaml` | Creates `truehear-backend` with automatic token mounting disabled |
| `workload/local-host/backend/kustomization.yaml`  | Builds the backend identity resources                             |
| `workload/local-host/backend/flux-ks.yaml`        | Reconciles the backend identity after Vault                       |

<!-- markdownlint-enable MD013 -->

The backend component does not deploy the application yet. It creates only the
identity required for Vault Kubernetes authentication.

## Prerequisites

Complete the local-host bootstrap and pivot first. These resources must
already exist:

- `local-management` cluster;
- `local-workload` cluster;
- `krops-registry` container;
- management and workload Flux instances;
- `.kube/krops-mgmt-host.yaml`;
- `local-workload.kubeconfig`.

Run all commands from the repository root:

```bash
cd /Users/sameer/sameer/truehear-infra-dev
```

Confirm the branch before making a change:

```bash
git status --short --branch
```

## Step 1: Select the workload cluster

Use the generated workload kubeconfig:

```bash
export KUBECONFIG="$PWD/local-workload.kubeconfig"
kubectl get nodes
```

Expected: one Ready control-plane node and one Ready worker node.

This kubeconfig points the macOS client at the workload API port published by
Docker. It is separate from the management kubeconfig because the two files
authenticate to two different Kubernetes APIs.

## Step 2: Verify workload storage

The storage definition is reconciled by management Flux and delivered to the
workload cluster through a `ClusterResourceSet`.

Verify the management-side Kustomization:

```bash
KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
mise exec -- flux get kustomizations
```

Expected: `local-workload-storage` is Ready.

Verify the workload-side resources:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get storageclass

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods --namespace local-path-storage
```

Expected:

- `standard` is the default StorageClass;
- its provisioner is `rancher.io/local-path`;
- the local-path provisioner Pod is Running.

The local-path provisioner stores data inside the Docker workload node. This
is suitable for a local exercise, but it is not cloud storage and is not a
backup.

## Step 3: Prepare SOPS and age

Generate an age key only when `age.agekey` does not already exist:

```bash
test -f age.agekey || mise run sops-keygen
```

The private key stays in the gitignored `age.agekey` file. Its public recipient
is committed in `.sops.yaml` under the `workload/local-host` creation rule.

Confirm that the private key is ignored:

```bash
git check-ignore age.agekey
```

Workload Flux needs the private key because it decrypts
`vault-tls.sops.yaml`. Create the bootstrap Secret after every fresh workload
cluster creation:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl create secret generic sops-age \
  --namespace flux-system \
  --from-file=age.agekey=age.agekey \
  --dry-run=client \
  --output yaml |
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl apply --filename -
```

Confirm only its metadata, never its value:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get secret sops-age --namespace flux-system
```

This Secret is a bootstrap exception. It cannot be decrypted from the same
Git source using the key that it is supposed to provide.

## Step 4: Verify the encrypted Vault TLS Secret

The current certificate covers these names:

```text
vault
vault.vault.svc.cluster.local
vault-internal.vault.svc.cluster.local
vault-0.vault-internal.vault.svc.cluster.local
vault-1.vault-internal.vault.svc.cluster.local
vault-2.vault-internal.vault.svc.cluster.local
```

Confirm that the committed manifest is encrypted:

```bash
mise exec -- sops filestatus \
  workload/local-host/vault/vault-tls.sops.yaml
```

Expected: `encrypted` is `true`.

Validate decryption without printing plaintext:

```bash
SOPS_AGE_KEY_FILE=age.agekey \
mise run sops-decrypt workload/local-host/vault/vault-tls.sops.yaml |
kubectl create --dry-run=client --filename - --output name
```

Expected:

```text
secret/vault-tls
```

Do not commit the leaf private key, the age private key, or a decrypted Secret
manifest.

## Step 5: Publish the Vault configuration

Validate the relevant Kustomize trees:

```bash
kubectl kustomize workload/local-host/vault >/dev/null
kubectl kustomize workload/local-host >/dev/null
git diff --check
```

Publish the complete local-host management and workload trees:

```bash
mise -E local-host run oci-push
```

The task pushes a new immutable digest behind this local reference:

```text
oci://localhost:5001/krops:latest
```

Watch workload reconciliation:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux get kustomizations --watch
```

Wait for `vault` to report Ready at the new digest, then press `Ctrl+C`.

## Step 6: Verify the Vault deployment

Check the Helm release:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux get helmreleases --all-namespaces
```

Expected: the `vault/vault` release is Ready with chart version `0.34.1`.

Check the Pods and persistent claims:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods --namespace vault --output wide

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pvc --namespace vault
```

Before initialization, the Vault Pods may show `0/1 Running`. That means the
containers are running but Vault is sealed. It is expected at this stage.

Expected PVCs:

```text
data-vault-0
data-vault-1
data-vault-2
```

Each claim should be Bound to the `standard` StorageClass.

## Step 7: Initialize Vault

Initialize exactly one member. Do not initialize every Pod independently.

Choose the protected output location:

```bash
vault_init_dir="/Users/sameer/sameer/cloud-infra-development-pulumi/secure/vault-local-workload"
mkdir -p "$vault_init_dir"
chmod 700 "$vault_init_dir"

vault_init_file="$vault_init_dir/initialization-$(date -u +%Y%m%dT%H%M%SZ).json"
umask 077
```

Initialize `vault-0` with five shares and a threshold of three:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace vault vault-0 -- \
  env \
    VAULT_ADDR="https://vault-0.vault-internal.vault.svc.cluster.local:8200" \
    VAULT_CACERT="/vault/userconfig/vault-tls/ca.crt" \
  vault operator init \
    -key-shares=5 \
    -key-threshold=3 \
    -format=json > "$vault_init_file"

chmod 600 "$vault_init_file"
echo "Vault initialization data saved without printing it: $vault_init_file"
```

Never display or commit this file. Keep the `vault_init_file` variable in the
same shell for the remaining steps.

## Step 8: Unseal all Vault members

Unseal `vault-0` with the first three shares:

```bash
for share_index in 0 1 2; do
  jq -er ".unseal_keys_b64[$share_index]" "$vault_init_file" |
    KUBECONFIG="$PWD/local-workload.kubeconfig" \
    kubectl exec -i --namespace vault vault-0 -- sh -c '
      IFS= read -r unseal_key
      VAULT_ADDR="https://vault-0.vault-internal.vault.svc.cluster.local:8200" \
      VAULT_CACERT="/vault/userconfig/vault-tls/ca.crt" \
      vault operator unseal "$unseal_key" >/dev/null
    '
done
```

The other members join the same integrated Raft cluster. They are initialized
but still sealed. Unseal both with the same threshold shares:

```bash
for vault_pod in vault-1 vault-2; do
  echo "Unsealing $vault_pod"

  for share_index in 0 1 2; do
    jq -er ".unseal_keys_b64[$share_index]" "$vault_init_file" |
      KUBECONFIG="$PWD/local-workload.kubeconfig" \
      kubectl exec -i --namespace vault "$vault_pod" -- \
        env VAULT_POD_NAME="$vault_pod" sh -c '
          IFS= read -r unseal_key
          VAULT_DNS="${VAULT_POD_NAME}.vault-internal.vault.svc.cluster.local"
          VAULT_ADDR="https://${VAULT_DNS}:8200" \
          VAULT_CACERT="/vault/userconfig/vault-tls/ca.crt" \
          vault operator unseal "$unseal_key" >/dev/null
        '
  done
done
```

Verify readiness:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods --namespace vault
```

Expected: all three Pods are `1/1 Running`.

## Step 9: Verify the Raft cluster

Use the root token without printing it:

```bash
jq -er '.root_token' "$vault_init_file" |
  KUBECONFIG="$PWD/local-workload.kubeconfig" \
  kubectl exec -i --namespace vault vault-0 -- sh -c '
    IFS= read -r VAULT_TOKEN
    export VAULT_TOKEN
    export VAULT_ADDR="https://vault-0.vault-internal.vault.svc.cluster.local:8200"
    export VAULT_CACERT="/vault/userconfig/vault-tls/ca.crt"
    vault operator raft list-peers -format=json
  ' |
  jq '.data.config.servers[] | {node_id, address, leader, voter}'
```

Expected:

- three Raft members;
- exactly one leader;
- all three members are voters.

This proves that the Pods are one replicated Vault cluster, not three separate
Vault installations.

## Step 10: Verify Vault can review Kubernetes JWTs

The Vault Helm chart creates `vault-server-binding`. It gives the
`vault/vault` ServiceAccount permission to submit TokenReview requests.

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get clusterrolebinding vault-server-binding

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl auth can-i create tokenreviews.authentication.k8s.io \
  --as=system:serviceaccount:vault:vault

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace vault vault-0 -- sh -c '
  test -r /var/run/secrets/kubernetes.io/serviceaccount/token &&
  test -r /var/run/secrets/kubernetes.io/serviceaccount/ca.crt &&
  echo "Vault reviewer credentials: OK"
'
```

Expected:

```text
yes
Vault reviewer credentials: OK
```

## Step 11: Publish the backend identity

The backend identity is Git-managed under `workload/local-host/backend`. Its
ServiceAccount has automatic token mounting disabled. A future Deployment must
explicitly project a short-lived token with audience `vault`.

Publish the current configuration:

```bash
mise -E local-host run oci-push
```

Wait for the backend Flux Kustomization:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux get kustomizations --watch
```

Verify the identity:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get serviceaccount truehear-backend \
  --namespace backend \
  --output jsonpath='{.metadata.name}{" automount="}{.automountServiceAccountToken}{"\n"}'
```

Expected:

```text
truehear-backend automount=false
```

## Step 12: Enable Vault Kubernetes authentication

This is Vault runtime configuration stored in Raft. It is not a Kubernetes
manifest reconciled by Flux.

```bash
jq -er '.root_token' "$vault_init_file" |
  KUBECONFIG="$PWD/local-workload.kubeconfig" \
  kubectl exec -i --namespace vault vault-0 -- sh -c '
    IFS= read -r VAULT_TOKEN
    test -n "$VAULT_TOKEN" || exit 1
    export VAULT_TOKEN
    export VAULT_ADDR="https://vault-0.vault-internal.vault.svc.cluster.local:8200"
    export VAULT_CACERT="/vault/userconfig/vault-tls/ca.crt"

    if vault auth list -format=json | grep -q "\"kubernetes/\""; then
      echo "Kubernetes auth already enabled"
    else
      vault auth enable kubernetes
    fi

    vault write auth/kubernetes/config \
      kubernetes_host="https://kubernetes.default.svc:443"

    printf "Configured Kubernetes API: "
    vault read -field=kubernetes_host auth/kubernetes/config
    echo
  '
```

Vault uses its own mounted ServiceAccount token and CA certificate. Do not
copy a long-lived reviewer JWT into the Vault configuration.

## Step 13: Create the backend policy

The policy permits one KV v2 read and self-revocation only:

```bash
{
  jq -er '.root_token' "$vault_init_file"

  cat <<'HCL'
path "secret/data/app/truehear" {
  capabilities = ["read"]
}

path "auth/token/revoke-self" {
  capabilities = ["update"]
}
HCL
} |
  KUBECONFIG="$PWD/local-workload.kubeconfig" \
  kubectl exec -i --namespace vault vault-0 -- sh -c '
    IFS= read -r VAULT_TOKEN
    test -n "$VAULT_TOKEN" || exit 1
    export VAULT_TOKEN
    export VAULT_ADDR="https://vault-0.vault-internal.vault.svc.cluster.local:8200"
    export VAULT_CACERT="/vault/userconfig/vault-tls/ca.crt"

    vault policy write truehear-backend-dev -
    vault policy read truehear-backend-dev
  '
```

## Step 14: Create the backend Vault role

The role accepts only the intended ServiceAccount, namespace, and audience:

```bash
{
  jq -er '.root_token' "$vault_init_file"

  cat <<'JSON'
{
  "bound_service_account_names": ["truehear-backend"],
  "bound_service_account_namespaces": ["backend"],
  "audience": "vault",
  "token_policies": ["truehear-backend-dev"],
  "token_ttl": "1h",
  "token_explicit_max_ttl": "1h",
  "token_no_default_policy": true
}
JSON
} |
  KUBECONFIG="$PWD/local-workload.kubeconfig" \
  kubectl exec -i --namespace vault vault-0 -- sh -c '
    IFS= read -r VAULT_TOKEN
    test -n "$VAULT_TOKEN" || exit 1
    export VAULT_TOKEN
    export VAULT_ADDR="https://vault-0.vault-internal.vault.svc.cluster.local:8200"
    export VAULT_CACERT="/vault/userconfig/vault-tls/ca.crt"

    vault write auth/kubernetes/role/truehear-backend-dev -
    vault read auth/kubernetes/role/truehear-backend-dev
  '
```

The resulting relationship is:

```text
backend/truehear-backend JWT with audience vault
                         ↓
Vault role truehear-backend-dev
                         ↓
Vault policy truehear-backend-dev
```

## Step 15: Create a test secret

Enable a KV v2 engine at `secret/` if it does not exist. Create the dummy path
only when it is empty:

```bash
jq -er '.root_token' "$vault_init_file" |
  KUBECONFIG="$PWD/local-workload.kubeconfig" \
  kubectl exec -i --namespace vault vault-0 -- sh -c '
    set -e
    IFS= read -r VAULT_TOKEN
    test -n "$VAULT_TOKEN" || exit 1
    export VAULT_TOKEN
    export VAULT_ADDR="https://vault-0.vault-internal.vault.svc.cluster.local:8200"
    export VAULT_CACERT="/vault/userconfig/vault-tls/ca.crt"

    if vault secrets list -format=json | grep -q "\"secret/\""; then
      echo "secret/ engine already exists"
    else
      vault secrets enable -path=secret -version=2 kv
    fi

    vault kv put -cas=0 -mount=secret app/truehear dummy=dummy
    vault kv metadata get -mount=secret app/truehear
  '
```

`-cas=0` means create only. It refuses to overwrite existing application
data.

## Step 16: Test the complete identity chain

This test uses no root token. It requests a ten-minute Kubernetes JWT,
exchanges it for a restricted Vault token, reads the dummy value, and revokes
the Vault token.

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl create token truehear-backend \
  --namespace backend \
  --audience vault \
  --duration 10m |
  KUBECONFIG="$PWD/local-workload.kubeconfig" \
  kubectl exec -i --namespace vault vault-0 -- sh -c '
    IFS= read -r backend_jwt
    test -n "$backend_jwt" || exit 1

    export VAULT_ADDR="https://vault-0.vault-internal.vault.svc.cluster.local:8200"
    export VAULT_CACERT="/vault/userconfig/vault-tls/ca.crt"

    backend_token="$(
      printf "%s" "$backend_jwt" |
        vault write -field=token \
          auth/kubernetes/login \
          role=truehear-backend-dev \
          jwt=-
    )"
    unset backend_jwt

    test -n "$backend_token" || exit 1
    echo "Vault login: OK"

    export VAULT_TOKEN="$backend_token"
    unset backend_token

    test_value="$(vault kv get -field=dummy -mount=secret app/truehear)"
    test "$test_value" = "dummy"
    unset test_value
    echo "Restricted secret read: OK"

    vault token revoke -self >/dev/null
    echo "Vault token self-revocation: OK"
  '
```

Expected:

```text
Vault login: OK
Restricted secret read: OK
Vault token self-revocation: OK
```

## Step 17: Test from a backend running on macOS

Open an HTTPS tunnel in one terminal:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl port-forward --namespace vault pod/vault-0 8200:8200
```

In another terminal, generate a short-lived JWT:

```bash
backend_jwt_path="$(mktemp /tmp/truehear-backend-vault-jwt.XXXXXX)"
chmod 600 "$backend_jwt_path"

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl create token truehear-backend \
  --namespace backend \
  --audience vault \
  --duration 30m > "$backend_jwt_path"

export VAULT_KUBERNETES_JWT_PATH="$backend_jwt_path"
echo "JWT created at: $VAULT_KUBERNETES_JWT_PATH"
```

Use these backend settings:

```dotenv
VAULT_AUTH_MODE=kubernetes
VAULT_SECURE_CONNECTION_ENABLED=true
VAULT_KUBERNETES_ROLE=truehear-backend-dev
VAULT_KUBERNETES_JWT_PATH=/tmp/truehear-backend-vault-jwt.REPLACE_ME
VAULT_URL=https://127.0.0.1:8200
VAULT_TLS_SERVERNAME=vault.vault.svc.cluster.local
VAULT_CA_CRT=/absolute/path/to/the/current/ca-chain.pem
VAULT_CLIENT_CRT=
VAULT_CLIENT_KEY=
VAULT_CRL=
VAULT_KV_MOUNT=secret
VAULT_KV_VERSION=v2
VAULT_REQUEST_TIMEOUT_MS=10000
VAULT_PATH=app/truehear
```

Do not set `VAULT_TOKEN`, `VAULT_ROLE_ID`, or `VAULT_SECRET_ID` in Kubernetes
authentication mode. Those select different authentication models.

The backend connects to `127.0.0.1` through the tunnel but verifies the server
certificate against `vault.vault.svc.cluster.local`. Replace the temporary JWT
after it expires. A real Pod uses a projected token volume that Kubernetes
rotates automatically.

Remove the temporary JWT after the local test:

```bash
unset VAULT_KUBERNETES_JWT_PATH
rm "$backend_jwt_path"
unset backend_jwt_path
```

## Day-2 update workflow

Persistent Kubernetes changes must be made in Git-managed YAML:

```text
Edit YAML
   ↓
Build and validate Kustomize
   ↓
Push a new local OCI artifact
   ↓
Flux observes a new digest
   ↓
Flux reconciles the management or workload cluster
```

Use:

```bash
git diff --check
kubectl kustomize mgmt/local-host >/dev/null
kubectl kustomize workload/local-host >/dev/null
mise run validate
mise -E local-host run oci-push
```

Then inspect both reconciliation layers:

```bash
KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
mise exec -- flux get kustomizations

KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux get all --all-namespaces
```

Follow [rules.md](rules.md) before adding a new management addon, workload
application, secret, or air-gap dependency.

## Restart and recovery behavior

- Raft data survives a normal Vault Pod restart while its PVC survives.
- A restarted member becomes sealed and must be unsealed again when Shamir
  sealing is used.
- Any three of the five shares can unseal a member.
- Do not initialize a replacement member as a separate Vault cluster.
- Local-path volumes live inside Docker nodes. Deleting the CAPD workload
  cluster can delete the local data even when the PVC retention policy says
  `Retain`.
- Git and SOPS can recreate the Kubernetes resources and TLS Secret, but they
  do not recreate Vault's Raft data.
- Do not restart, upgrade, or tear down the Vault cluster unless the unseal
  shares are available.

## Troubleshooting

### Vault Pods are Running but not Ready

Check Vault status:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace vault vault-0 -- \
  env \
    VAULT_ADDR="https://vault-0.vault-internal.vault.svc.cluster.local:8200" \
    VAULT_CACERT="/vault/userconfig/vault-tls/ca.crt" \
  vault status
```

Exit code `2` with `Sealed: true` means Vault needs unseal shares. It is not a
Helm or Kubernetes deployment failure.

### Flux cannot decrypt the TLS Secret

Check the workload secret and Kustomization:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get secret sops-age --namespace flux-system

KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux get kustomization vault
```

Confirm that the private age key matches the public recipient in `.sops.yaml`.

### The OCI digest changed but Vault did not

Check that both `flux-system` and `vault` report the new digest:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux get kustomizations
```

The normal local artifact includes the complete workload tree. A missing
Kustomization registration in `workload/local-host/kustomization.yaml` can
still prevent a new component from being created.

### Kubernetes login is denied

Check all four identity fields:

| Field          | Required value         |
| -------------- | ---------------------- |
| Namespace      | `backend`              |
| ServiceAccount | `truehear-backend`     |
| JWT audience   | `vault`                |
| Vault role     | `truehear-backend-dev` |

Generate a fresh JWT if the local file has expired.

### Login succeeds but the secret read is denied

Check that the role attaches `truehear-backend-dev` and that the policy path is
the KV v2 API path:

```text
secret/data/app/truehear
```

The application-facing logical path remains `app/truehear` with mount
`secret`.

### macOS reports a TLS hostname error

For the port-forwarded backend test, use both:

```dotenv
VAULT_URL=https://127.0.0.1:8200
VAULT_TLS_SERVERNAME=vault.vault.svc.cluster.local
```

Also use the current CA chain. Do not disable verification.

## Completion checklist

- [ ] `standard` is the default workload StorageClass.
- [ ] The local-path provisioner is Running.
- [ ] Workload Flux has the `sops-age` Secret.
- [ ] `vault-tls.sops.yaml` remains encrypted in Git.
- [ ] The Vault HelmRelease is Ready.
- [ ] All three Vault PVCs are Bound.
- [ ] All three Vault Pods are `1/1 Running` after unsealing.
- [ ] Raft reports three voters and exactly one leader.
- [ ] `backend/truehear-backend` exists with automount disabled.
- [ ] Vault Kubernetes authentication points at the in-cluster API.
- [ ] The backend role binds the correct namespace, ServiceAccount, and
      audience.
- [ ] The manual JWT test logs in, reads the dummy value, and self-revokes.
- [ ] The local backend succeeds through verified HTTPS port-forwarding.
- [ ] No age private key, root token, unseal share, JWT, or plaintext TLS key
      is tracked by Git.
