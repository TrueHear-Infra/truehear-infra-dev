# Local-host backend deployment runbook

This runbook explains how the TrueHear backend image is delivered to the local
KROPS workload cluster, how the Pod receives configuration and identity, and
how to test a Flux CD update without merging to GitHub.

The repeatable traffic-distribution procedure is documented separately in
[Local backend load-balancing test](backend-load-balancing-test.md).

## Table of contents

- [Goal](#goal)
- [Architecture](#architecture)
- [Deployment versus StatefulSet](#deployment-versus-statefulset)
- [Backend Service and load balancing](#backend-service-and-load-balancing)
- [Security rules](#security-rules)
- [Repository files](#repository-files)
- [Step 1: Verify the backend image](#step-1-verify-the-backend-image)
- [Step 2: Prepare the GHCR pull Secret](#step-2-prepare-the-ghcr-pull-secret)
- [Step 3: Prepare the backend CA bundle](#step-3-prepare-the-backend-ca-bundle)
- [Step 4: Prepare the backend environment](#step-4-prepare-the-backend-environment)
- [Step 5: Configure the backend identity](#step-5-configure-the-backend-identity)
- [Step 6: Understand the Deployment](#step-6-understand-the-deployment)
- [Step 7: Register the backend with Flux](#step-7-register-the-backend-with-flux)
- [Step 8: Validate the configuration](#step-8-validate-the-configuration)
- [Step 9: Publish the workload artifact](#step-9-publish-the-workload-artifact)
- [Step 10: Reconcile Flux](#step-10-reconcile-flux)
- [Step 11: Verify the backend Pod](#step-11-verify-the-backend-pod)
- [Simulate a Git merge through local Flux](#simulate-a-git-merge-through-local-flux)
- [Normal GitHub flow](#normal-github-flow)
- [Troubleshooting](#troubleshooting)
- [Completion checklist](#completion-checklist)

## Goal

The completed local deployment provides:

- Replaceable backend Pods managed by a Kubernetes Deployment.
- One stable ClusterIP Service that selects every ready backend Pod.
- A private multi-platform image pulled from GHCR by immutable digest.
- Non-secret configuration mounted as `/app/.env`.
- A public CA bundle mounted at `/etc/truehear/tls/ca.crt`.
- A short-lived Kubernetes JWT projected specifically for Vault.
- Application secrets retrieved from Vault at runtime.
- Workload delivery through the local OCI registry and Flux.

The backend does not store application passwords, private keys, or Vault
tokens in its Git-managed manifests.

## Architecture

```text
Backend source repository
        |
        | GitHub Actions builds a multi-platform image
        v
Private GHCR image
        |
        | imagePullSecret authenticates the worker
        v
Backend Pod in the local workload cluster
        |
        +--> /app/.env from ConfigMap
        +--> /etc/truehear/tls/ca.crt from ConfigMap
        +--> /var/run/secrets/vault/token from Kubernetes
        +--> runtime application secrets from Vault
```

The local GitOps delivery path is:

```text
workload/local-host/backend YAML
        |
        | mise -E local-host run oci-push
        v
oci://localhost:5001/krops:latest
        |
        | workload source-controller downloads the artifact
        v
workload kustomize-controller
        |
        +--> decrypts ghcr-credentials.sops.yaml
        +--> applies the backend resources
        v
Deployment controller creates the backend Pod
```

The local OCI artifact replaces the GitHub `main` branch only for this local
exercise. The reconciliation principle is the same.

## Deployment versus StatefulSet

The backend uses a Deployment because every backend Pod is interchangeable.
Its Pod name contains a ReplicaSet hash and a random suffix:

```text
truehear-backend-7b59dcb6b5-r2qbg
```

Scaling to three replicas produces three replaceable Pods with different
suffixes. A failed Pod can be deleted and replaced without preserving its
identity or local disk.

Vault, RabbitMQ, and Redis use StatefulSets because their members need stable
identities such as:

```text
vault-0
rabbitmq-1
redis-5
```

Use this rule:

```text
Replaceable application process  -> Deployment
Stable member identity or volume -> StatefulSet
```

## Backend Service and load balancing

The backend Service provides one stable address in front of all ready backend
Pods:

```text
Service/truehear-backend:8887
        |
        | selector: app.kubernetes.io/name=truehear-backend
        v
EndpointSlice
├── backend Pod 1:8887
├── backend Pod 2:8887
└── backend Pod 3:8887
```

The Service does not contain the Pod names or IP addresses. Kubernetes watches
for ready Pods whose labels match the Service selector and maintains an
EndpointSlice automatically. When a Pod is replaced or the Deployment is
scaled, the endpoint list changes automatically.

`type: ClusterIP` means the Service is reachable only inside the Kubernetes
cluster. Internal callers use this stable DNS name:

```text
truehear-backend.backend.svc.cluster.local:8887
```

For local Mac access, `kubectl port-forward` creates a temporary tunnel:

```text
Mac localhost:8887
        |
        | kubectl port-forward
        v
Service/truehear-backend:8887
        |
        v
One ready backend Pod
```

In AWS, an external Application Load Balancer or another ingress implementation
would sit in front of the Service:

```text
Internet or private client
        |
        v
AWS Application Load Balancer
        |
        v
Kubernetes Ingress or Gateway
        |
        v
Service/truehear-backend
        |
        +--> backend Pod 1
        +--> backend Pod 2
        +--> backend Pod 3
```

The Pods are not registered manually. Kubernetes and the AWS load-balancer
controller maintain the routing targets from the declared Service and ingress
configuration.

## Security rules

- Never commit a GHCR token or plaintext Docker configuration.
- Keep `ghcr-credentials.sops.yaml` encrypted with SOPS.
- Never commit the age private key.
- Keep passwords, signing secrets, and database credentials in Vault.
- The `.env` ConfigMap may contain only non-secret settings.
- Pin the backend image by digest instead of using `latest`.
- Do not commit a manually generated backend JWT.
- Let Kubernetes project and rotate the Vault JWT.
- Do not use `kubectl apply` or `kubectl scale` for persistent changes.

## Repository files

```text
workload/local-host/backend/
├── namespace.yaml
├── serviceaccount.yaml
├── ghcr-credentials.sops.yaml
├── ca-bundle.yaml
├── configmap.yaml
├── deployment.yaml
├── service.yaml
├── kustomization.yaml
└── flux-ks.yaml
```

| File                           | Purpose                                  |
| ------------------------------ | ---------------------------------------- |
| `namespace.yaml`             | Creates the`backend` namespace.        |
| `serviceaccount.yaml`        | Gives the Pod its Kubernetes identity.   |
| `ghcr-credentials.sops.yaml` | Stores the encrypted GHCR pull login.    |
| `ca-bundle.yaml`             | Stores the public PKI trust chain.       |
| `configmap.yaml`             | Provides the non-secret`.env` file.    |
| `deployment.yaml`            | Defines the backend Pod and mounts.      |
| `service.yaml`               | Provides stable networking for the Pods. |
| `kustomization.yaml`         | Lists the resources in this component.   |
| `flux-ks.yaml`               | Tells Flux how and when to reconcile it. |

The workload root registers `backend/flux-ks.yaml` in
`workload/local-host/kustomization.yaml`.

## Step 1: Verify the backend image

Authenticate to GHCR without printing the token:

```bash
env -u GITHUB_TOKEN gh auth token |
docker login ghcr.io \
  --username Sameerbk201 \
  --password-stdin
```

Inspect the published multi-platform image:

```bash
docker buildx imagetools inspect \
  ghcr.io/truehear/truehearv3-backend:sha-8b954c4
```

Confirm that the index contains runnable `linux/amd64` and `linux/arm64`
manifests. The local CAPD worker uses ARM64, while a future x86 EKS worker can
select AMD64 from the same image index.

The Deployment pins the top-level image index digest:

```text
ghcr.io/truehear/truehearv3-backend@sha256:0fc89c43c2aa651be2efe7afbdeeb2df3cee2783353eba49760c53932edf7cc5
```

When publishing a newer backend image, inspect it again and update this digest
in `deployment.yaml`.

## Step 2: Prepare the GHCR pull Secret

The private package requires a GitHub credential with package read access.
Create the Kubernetes Secret locally, then encrypt it before it enters the
repository.

Use a protected temporary file:

```bash
secret_file=$(mktemp)
chmod 600 "$secret_file"

read -s "ghcr_token?GHCR package-read token: "
echo

kubectl create secret docker-registry ghcr-credentials \
  --namespace backend \
  --docker-server ghcr.io \
  --docker-username Sameerbk201 \
  --docker-password "$ghcr_token" \
  --dry-run=client \
  --output yaml >"$secret_file"

unset ghcr_token
```

Copy it to the SOPS manifest path and encrypt it immediately:

```bash
cp "$secret_file" \
  workload/local-host/backend/ghcr-credentials.sops.yaml

mise run sops-encrypt \
  workload/local-host/backend/ghcr-credentials.sops.yaml

rm -f "$secret_file"
unset secret_file
```

Confirm encryption without printing the credential:

```bash
mise exec -- sops filestatus \
  workload/local-host/backend/ghcr-credentials.sops.yaml
```

Expected: `encrypted` is `true`.

The Secret must have:

```yaml
metadata:
  name: ghcr-credentials
  namespace: backend
type: kubernetes.io/dockerconfigjson
```

The Deployment refers to it through:

```yaml
imagePullSecrets:
  - name: ghcr-credentials
```

The worker node uses this Secret to pull the image. Flux does not pull the
application image.

## Step 3: Prepare the backend CA bundle

`ca-bundle.yaml` contains the public CA chain used to verify Vault, RabbitMQ,
and Redis certificates. A CA certificate is public material, so it does not
need SOPS.

It creates:

```text
ConfigMap/truehear-ca-bundle
└── ca.crt
```

The Deployment mounts it as:

```text
/etc/truehear/tls/ca.crt
```

The backend `.env` points every TLS client at this mounted path.

## Step 4: Prepare the backend environment

`configmap.yaml` stores one file entry:

```yaml
data:
  .env: |
    PORT=8887
    ENVIRONMENT=development
    NODE_ENV=development
    # Remaining non-secret settings
```

The Deployment sets:

```yaml
env:
  - name: ENV_FILE
    value: .env
```

It mounts the ConfigMap entry with:

```yaml
volumeMounts:
  - name: backend-config
    mountPath: /app/.env
    subPath: .env
    readOnly: true
```

The final flow is:

```text
ConfigMap/truehear-backend-config key .env
        |
        | Kubernetes volume mount
        v
/app/.env
        |
        | backend dotenv loader
        v
process.env
```

Do not add passwords or tokens to this file. The backend obtains its runtime
secrets from Vault.

## Step 5: Configure the backend identity

The backend Pod uses:

```text
Namespace:      backend
ServiceAccount: truehear-backend
Vault role:     truehear-backend-dev
```

The ServiceAccount disables the normal automatic token mount:

```yaml
automountServiceAccountToken: false
```

The Deployment requests a dedicated projected token instead:

```yaml
projected:
  sources:
    - serviceAccountToken:
        path: token
        audience: vault
        expirationSeconds: 3600
```

Kubernetes generates and rotates this token. It appears inside the container
at:

```text
/var/run/secrets/vault/token
```

The backend sends the JWT and role name to Vault's Kubernetes login endpoint.
Vault verifies the namespace, ServiceAccount, audience, and role binding. It
then returns a short-lived Vault client token to the application.

No projected JWT is committed to Git.

## Step 6: Understand the Deployment

The local Deployment uses one replica:

```yaml
spec:
  replicas: 1
```

Its main runtime wiring is:

```text
Image
  ghcr.io/truehear/truehearv3-backend@sha256:...

Configuration
  /app/.env

Trust
  /etc/truehear/tls/ca.crt

Vault identity
  /var/run/secrets/vault/token

Application port
  8887

Stable in-cluster address
  truehear-backend.backend.svc.cluster.local:8887
```

The probes call:

```text
Startup:   /api/truehear/v2/health/live
Readiness: /api/truehear/v2/health/ready
Liveness:  /api/truehear/v2/health/live
```

The startup probe gives the process time to initialize. Readiness controls
whether the Pod may receive Service traffic. Repeated liveness failures cause
Kubernetes to restart the container.

The Pod runs as non-root user `1000` and drops all Linux capabilities.

## Step 7: Register the backend with Flux

The component `kustomization.yaml` lists:

```yaml
resources:
  - namespace.yaml
  - serviceaccount.yaml
  - ghcr-credentials.sops.yaml
  - ca-bundle.yaml
  - configmap.yaml
  - deployment.yaml
  - service.yaml
```

The Flux Kustomization points at the component directory and enables SOPS:

```yaml
spec:
  dependsOn:
    - name: vault
    - name: rabbitmq
    - name: redis
  path: ./workload/local-host/backend
  decryption:
    provider: sops
    secretRef:
      name: sops-age
```

The dependencies ensure Flux waits for the platform components before applying
the backend component.

After every fresh workload-cluster creation, confirm that Flux has its age
private key:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get secret sops-age --namespace flux-system
```

Do not print or decode that Secret.

## Step 8: Validate the configuration

Validate the backend component:

```bash
mise exec -- kubectl kustomize \
  workload/local-host/backend >/dev/null
```

Validate the workload root:

```bash
mise exec -- kubectl kustomize \
  workload/local-host >/dev/null
```

Check whitespace and patch integrity:

```bash
git diff --check
```

Run the complete repository validation before pushing:

```bash
mise run validate
```

If the known Python environment problem reports that `yaml` is missing, use:

```bash
MISE_PYTHON_UV_VENV_AUTO=source mise run validate
```

## Step 9: Publish the workload artifact

Publish the updated local-host configuration:

```bash
mise -E local-host run oci-push
```

The successful initial backend deployment published:

```text
localhost:5001/krops@sha256:b7ee74acb4ef7aaf52ac661792a36da1eb963fd5912fcf4a7ed24b8c73e776fd
```

This packages `mgmt/local-host` and `workload/local-host`. The workload Flux
instance watches the resulting OCI artifact.

## Step 10: Reconcile Flux

Force source-controller to fetch the new OCI revision:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux reconcile source oci flux-system \
  --namespace flux-system
```

This command only downloads the newest configuration artifact. It does not
apply the backend resources.

Reconcile the workload root:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux reconcile kustomization flux-system \
  --namespace flux-system \
  --with-source
```

This command re-reads the top-level `workload/local-host` index. That index
creates or updates component instructions such as the backend Flux
Kustomization.

Reconcile the backend component:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux reconcile kustomization backend \
  --namespace flux-system \
  --with-source
```

This command processes `workload/local-host/backend`, decrypts its SOPS Secret,
and applies its Namespace, ServiceAccount, ConfigMaps, Deployment, and Service.

The three commands therefore mean:

```text
1. Fetch the newest package.
2. Refresh the workload component index.
3. Apply the backend component.
```

The repeated `--with-source` checks are safe but partly redundant. They make
the practice sequence explicit. If the backend Kustomization already exists
and only a file inside the backend directory changed, reconciling the backend
with `--with-source` is normally enough.

Normal polling would eventually perform the same work. Explicit reconciliation
makes the local practice loop faster.

## Step 11: Verify the backend Pod

Watch the Pod:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods --namespace backend --watch
```

Expected progression:

```text
Pending -> ContainerCreating -> Running
```

The successful initial deployment produced one ready Pod:

```text
truehear-backend-7b59dcb6b5-r2qbg   1/1   Running
```

The suffix is expected for a Deployment.

Inspect the Deployment:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get deployment truehear-backend --namespace backend
```

Verify that the Service discovered every ready backend Pod:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get service,endpointslice --namespace backend
```

Open the Service locally:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl port-forward \
  --namespace backend \
  service/truehear-backend \
  8887:8887
```

Test the health endpoint from another terminal:

```bash
curl --fail --silent --show-error \
  http://127.0.0.1:8887/api/truehear/v2/health/live
```

Stream application logs:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl logs \
  --namespace backend \
  deployment/truehear-backend \
  --follow
```

Stop watching with `Ctrl-C`.

Confirm the files exist without printing their contents:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace backend deployment/truehear-backend -- \
sh -c 'test -f /app/.env &&
  test -f /etc/truehear/tls/ca.crt &&
  test -f /var/run/secrets/vault/token'
```

A zero exit status confirms all three mounts exist.

## Simulate a Git merge through local Flux

This exercise changes the desired replica count from one to two. It proves
that Flux, rather than a direct `kubectl` mutation, updates the workload.

### Record the current state

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get deployment truehear-backend --namespace backend

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods --namespace backend
```

Expected: one ready Pod.

### Change the Git-managed desired state

Edit `workload/local-host/backend/deployment.yaml`:

```yaml
spec:
  replicas: 2
```

Do not run `kubectl scale`.

### Validate and publish

```bash
mise exec -- kubectl kustomize \
  workload/local-host/backend >/dev/null

git diff --check

mise -E local-host run oci-push
```

The new OCI digest represents the new desired state. Publishing it is the
local equivalent of merging the YAML change into the branch watched by Flux.

### Reconcile and watch the rollout

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux reconcile source oci flux-system \
  --namespace flux-system

KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux reconcile kustomization flux-system \
  --namespace flux-system \
  --with-source

KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux reconcile kustomization backend \
  --namespace flux-system \
  --with-source
```

Watch the Pods:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods --namespace backend --watch
```

Expected: a second randomly named backend Pod reaches `1/1 Running`.

Confirm the final count:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get deployment truehear-backend \
  --namespace backend \
  --output custom-columns=NAME:.metadata.name,DESIRED:.spec.replicas,READY:.status.readyReplicas
```

Expected:

```text
NAME               DESIRED   READY
truehear-backend   2         2
```

To return to one Pod, change `replicas` back to `1`, validate, publish another
OCI artifact, and reconcile again. This keeps Git-managed YAML as the source of
truth.

## Normal GitHub flow

The local exercise uses:

```text
YAML edit
  -> local OCI push
  -> workload Flux reconciliation
  -> Kubernetes rollout
```

The cloud workflow uses:

```text
Feature branch
  -> pull request
  -> merge to main
  -> Flux detects the Git revision
  -> Kubernetes rollout
```

In both cases Flux compares the declared state with the live state and applies
the difference. The source changes from local OCI to GitHub, but the workload
manifests and Kubernetes behavior remain the same.

## Troubleshooting

### Flux cannot decrypt the GHCR Secret

Symptoms include a backend Kustomization decryption error or an `ENC[...]`
value reaching Kubernetes.

Verify the Flux decryption configuration:

```bash
sed -n '1,120p' workload/local-host/backend/flux-ks.yaml
```

Verify only the age Secret metadata:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get secret sops-age --namespace flux-system
```

### Pod reports ImagePullBackOff

Inspect the Pod events:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl describe pod --namespace backend \
  --selector app.kubernetes.io/name=truehear-backend
```

Check that `ghcr-credentials` exists and has the correct type:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get secret ghcr-credentials \
  --namespace backend \
  --output jsonpath='{.type}{"\n"}'
```

Expected:

```text
kubernetes.io/dockerconfigjson
```

Rotate and re-encrypt the package-read credential if it has expired.

### Pod reports CreateContainerConfigError

Check events:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl describe pod --namespace backend \
  --selector app.kubernetes.io/name=truehear-backend
```

Confirm these objects exist:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get configmap truehear-backend-config \
  truehear-ca-bundle --namespace backend
```

### Backend cannot authenticate to Vault

Confirm the projected token exists without printing it:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace backend deployment/truehear-backend -- \
test -s /var/run/secrets/vault/token
```

Then inspect backend and Vault logs. Check that the Vault role is bound to the
`truehear-backend` ServiceAccount in the `backend` namespace with audience
`vault`.

### Flux still shows the previous OCI digest

Reconcile the OCI source first, then the root and backend Kustomizations:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux reconcile source oci flux-system \
  --namespace flux-system

KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux reconcile kustomization flux-system \
  --namespace flux-system \
  --with-source

KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux reconcile kustomization backend \
  --namespace flux-system \
  --with-source
```

### Pod is Running but not Ready

Check the probe result and application logs:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl describe pod --namespace backend \
  --selector app.kubernetes.io/name=truehear-backend

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl logs --namespace backend deployment/truehear-backend
```

Verify Vault, RabbitMQ, and Redis are healthy before changing the probe.

## Completion checklist

- [ ] The image index supports ARM64 and AMD64.
- [ ] `deployment.yaml` pins the immutable image digest.
- [ ] `ghcr-credentials.sops.yaml` is encrypted.
- [ ] `sops-age` exists in workload Flux.
- [ ] `/app/.env` contains only non-secret settings.
- [ ] The CA bundle is mounted read-only.
- [ ] The Vault JWT is projected with audience `vault`.
- [ ] The backend Kustomization depends on Vault, RabbitMQ, and Redis.
- [ ] Backend and workload Kustomize builds succeed.
- [ ] Flux reports the backend Kustomization ready.
- [ ] The backend Deployment has one ready Pod.
- [ ] The backend Service selects every ready backend Pod.
- [ ] Application logs confirm the required service connections.
- [ ] Replica changes are made through YAML and Flux, not direct mutation.
