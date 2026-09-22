# Local-host KROPS practice log

This runbook records the local KROPS exercise completed on 22 September 2026.
It explains what was run, what each step proved, the problems encountered, and
how each problem was resolved.

The exercise used only Docker containers. It did not create or modify any AWS
resources.

## Table of contents

- [Goal](#goal)
- [Final result](#final-result)
- [Architecture tested](#architecture-tested)
- [Practice environment](#practice-environment)
- [Step 1: Check for a cluster name collision](#step-1-check-for-a-cluster-name-collision)
- [Step 2: Download the toolbox](#step-2-download-the-toolbox)
- [Step 3: Bootstrap the local environment](#step-3-bootstrap-the-local-environment)
- [Step 4: Confirm the pivot](#step-4-confirm-the-pivot)
- [Step 5: Inspect the Docker containers](#step-5-inspect-the-docker-containers)
- [Step 6: Access the management cluster](#step-6-access-the-management-cluster)
- [Step 7: Verify management Flux](#step-7-verify-management-flux)
- [Step 8: Export the workload kubeconfig](#step-8-export-the-workload-kubeconfig)
- [Step 9: Verify the workload cluster](#step-9-verify-the-workload-cluster)
- [Step 10: Open Podinfo](#step-10-open-podinfo)
- [Step 11: Test a day-2 Flux update](#step-11-test-a-day-2-flux-update)
- [Step 12: Tear down the environment](#step-12-tear-down-the-environment)
- [Problems and fixes](#problems-and-fixes)
- [Key takeaways](#key-takeaways)

## Goal

The goal was to understand the complete local GitOps flow before creating AWS
infrastructure:

```text
Local configuration
        ↓
Local OCI registry
        ↓
Management Flux
        ↓
Local Kubernetes infrastructure
        ↓
Workload Flux
        ↓
Podinfo application
```

The local OCI registry replaces GitHub for this exercise. Publishing a new OCI
artifact is the local equivalent of merging a change into the branch watched by
Flux.

## Final result

The exercise completed successfully:

- The management and workload clusters were created in Docker.
- The bootstrap cluster transferred ownership to `local-management`.
- Management Flux reconciled every infrastructure Kustomization.
- Workload Flux installed Podinfo using Helm.
- Podinfo was reachable at `http://localhost:9898`.
- Podinfo was scaled from one replica to two through OCI and Flux.
- Flux applied OCI revision `sha256:18ca2c13...`.
- The workload cluster, management cluster, and local registry were removed by
  the controlled teardown.
- The unrelated `truehear-local` kind cluster was outside the KROPS lifecycle.

## Architecture tested

```text
Docker Desktop
│
├── local-management
│   ├── Management Flux
│   ├── Cluster API
│   ├── CAPD
│   ├── cert-manager
│   └── Addon controllers
│
├── local-workload
│   ├── Workload Flux
│   ├── One control-plane node
│   ├── One worker node
│   └── Podinfo
│
└── krops-registry
    └── OCI configuration artifact
```

The responsibility boundary was:

```text
Management cluster → manages infrastructure
Workload cluster   → runs applications
```

Both clusters run controller pods. The difference is what those controllers
manage.

## Practice environment

| Item | Value |
| --- | --- |
| Repository | `TrueHear-Infra/truehear-infra-dev` |
| Branch | `test-run-book/local-host-test` |
| Profile | `local-host` |
| Container engine | Docker Desktop on Apple Silicon |
| Local toolbox | `krops-toolbox:dev` |
| Kubernetes version | `v1.37.0` |
| Podinfo chart | `6.15.0` |

## Step 1: Check for a cluster name collision

KROPS uses a temporary kind cluster named `mgmt`. The name had to be free
because that cluster is deleted after the pivot.

```bash
mise exec -- kind get clusters
```

Observed output:

```text
truehear-local
```

There was no cluster named `mgmt`, so bootstrap could continue safely.

## Step 2: Download the toolbox

The published toolbox was downloaded from GitHub Container Registry:

```bash
docker pull ghcr.io/polarsquad/krops-toolbox:latest
```

The toolbox is a temporary administration environment containing the pinned
commands needed by KROPS. It automates the bootstrap and pivot, then exits.
Flux remains in the clusters and performs continuous reconciliation.

The downloaded image had this digest:

```text
sha256:e69e99802a02e9330dfe09dd267b84579c65d3bbe5bb98cc07fa368b5bccc43c
```

## Step 3: Bootstrap the local environment

### First attempt: Docker socket mount failure

The first command was:

```bash
scripts/toolbox-run.sh bootstrap local-host
```

It failed while mounting:

```text
/Users/sameer/.docker/run/docker.sock
```

Docker Desktop was using the `desktop-linux` context. The wrapper discovered
the user-directory socket, but Docker Desktop could not mount that path into
the toolbox.

The contexts were inspected with:

```bash
docker context show
docker context inspect --format '{{(index .Endpoints "docker").Host}}'
ls -l /var/run/docker.sock
```

The standard socket reached the same Docker engine, so the `default` context
was selected for individual commands:

```bash
DOCKER_CONTEXT=default scripts/toolbox-run.sh bootstrap local-host
```

### Second attempt: old Mise inside the published image

The socket problem was fixed, but the toolbox then reported:

```text
Mise required by repository: 2026.9.12
Mise inside published image:  2026.9.7
```

The host already had the correct version. The problem was limited to the
published Linux toolbox image.

The current checkout's Dockerfile pinned Mise `2026.9.12`, so a local toolbox
was built:

```bash
DOCKER_CONTEXT=default docker build \
  -f bootstrap-rs/Dockerfile \
  -t krops-toolbox:dev \
  .
```

Bootstrap was then run with the local image:

```bash
DOCKER_CONTEXT=default \
TOOLBOX_IMAGE=krops-toolbox:dev \
scripts/toolbox-run.sh bootstrap local-host
```

The toolbox performed these operations:

```text
Create temporary mgmt kind cluster
        ↓
Create local OCI registry
        ↓
Publish local-host configuration
        ↓
Install Flux, CAPI, and CAPD
        ↓
Create local-management and local-workload
        ↓
Install workload Flux and Podinfo
        ↓
Pivot ownership to local-management
        ↓
Delete temporary mgmt cluster
```

## Step 4: Confirm the pivot

Bootstrap ended with:

```text
Pivot complete: the management cluster is self-managed.
```

This meant that the Cluster API ownership objects had moved from the temporary
`mgmt` cluster to `local-management`. The temporary cluster was no longer
needed.

After the pivot:

```text
local-management
├── manages itself
└── manages local-workload
```

## Step 5: Inspect the Docker containers

The containers attached to the `kind` network were listed with:

```bash
DOCKER_CONTEXT=default docker ps \
  --filter network=kind \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

The important containers were:

| Container group | Purpose |
| --- | --- |
| `local-management-*` | Management control plane and worker |
| `local-management-lb` | Management Kubernetes API load balancer |
| `local-workload-*` | Workload control plane and worker |
| `local-workload-lb` | Workload Kubernetes API load balancer |
| `krops-registry` | Local OCI configuration registry |
| `truehear-local-control-plane` | Existing unrelated kind cluster |

The missing temporary `mgmt` container confirmed that pivot cleanup had run.

## Step 6: Access the management cluster

The toolbox-generated management kubeconfig contained an internal Docker
address:

```text
https://172.26.0.7:6443
```

That address worked inside the Docker `kind` network but was not routable from
macOS. A host `kubectl` call therefore timed out.

Docker had already published the management API on port `55002`. A separate
host kubeconfig was created so the toolbox kubeconfig remained unchanged:

```bash
cp .kube/krops-mgmt.yaml .kube/krops-mgmt-host.yaml

kubectl --kubeconfig .kube/krops-mgmt-host.yaml \
  config set-cluster local-management \
  --server=https://127.0.0.1:55002

export KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml"
```

The assigned port can change after a new bootstrap. Find the current port with:

```bash
DOCKER_CONTEXT=default docker port local-management-lb 6443/tcp
```

## Step 7: Verify management Flux

Management Flux was checked with:

```bash
kubectl get kustomizations -n flux-system
mise exec -- flux get kustomizations --watch
```

Every management Kustomization reported `READY=True`, including:

- `capi-system`
- `capd-system`
- `caaph-system`
- `cert-manager`
- `docker-management-cluster`
- `docker-workload-cluster`
- `flux-apps`
- `local-workload-cni`

This proved that management Flux had applied the local infrastructure
configuration.

## Step 8: Export the workload kubeconfig

The workload access file was exported through the management cluster:

```bash
mise -E local-host run kubeconfigs
```

This created the gitignored file:

```text
local-workload.kubeconfig
```

The helper also rewrote the workload API endpoint to a host-accessible
`127.0.0.1` port.

## Step 9: Verify the workload cluster

The workload nodes were checked with:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" kubectl get nodes
```

Both nodes were Ready:

```text
One control-plane node
One worker node
```

Workload Flux was checked with:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux get all --all-namespaces
```

The output confirmed:

- The workload OCI source was Ready.
- The Podinfo chart source was Ready.
- The Podinfo HelmRelease was Ready.
- The workload root Kustomization was Ready.

## Step 10: Open Podinfo

The application was exposed locally with:

```bash
mise -E local-host run podinfo-port-forward
```

Podinfo was then opened at:

```text
http://localhost:9898
```

The request path was:

```text
Browser
  → localhost:9898
  → kubectl port-forward
  → Podinfo Service
  → Podinfo pod
```

Stopping the port-forward with Ctrl-C removed only browser access. It did not
stop Podinfo or either cluster.

## Step 11: Test a day-2 Flux update

### Record the initial application state

The initial Podinfo deployment contained one replica:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get deployment,pods -n podinfo
```

### Change the desired state

In `workload/local-host/podinfo/helm.yaml`, this value was changed:

```yaml
replicaCount: 1
```

to:

```yaml
replicaCount: 2
```

No direct `kubectl scale` or `kubectl apply` command was used.

### Validate the repository

The first validation attempt failed because the Python selected by Mise could
not import the `yaml` module:

```text
ModuleNotFoundError: No module named 'yaml'
```

The Podinfo Kustomize build had already passed. The failure came from an
unrelated air-gap test, but the complete validation was still required.

Running `uv run mise run validate` did not fix it because the nested Mise
process selected its standalone Python instead of `.venv`. The existing uv
environment was explicitly sourced by Mise:

```bash
MISE_PYTHON_UV_VENV_AUTO=source mise run validate
```

Validation then completed without errors.

### Publish the new OCI artifact

The changed local-host configuration was published with:

```bash
mise -E local-host run oci-push
```

The new artifact digest was:

```text
sha256:18ca2c1347302ab7c4e9d438aea92e6c410b2a6cf0d4eaaa8e7d9db4bef39971
```

### Watch the application update

Watching multiple resource types together with `--watch` produced:

```text
error: you may only specify a single resource type
```

The fix was to watch only the pods:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods -n podinfo --watch
```

Two Podinfo pods reached `Running`. One pod was the existing pod and the second
had just been created.

The final Flux check was:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux get all --all-namespaces
```

It showed:

```text
OCI revision:       latest@sha256:18ca2c13...
Podinfo HelmRelease: Ready=True
Helm result:         upgrade succeeded
Release revision:    podinfo.v2
```

This proved the complete local day-2 flow:

```text
YAML edit
  → OCI publication
  → Flux reconciliation
  → Helm upgrade
  → Kubernetes rollout
```

## Step 12: Tear down the environment

The wrapper teardown was attempted first:

```bash
DOCKER_CONTEXT=default \
TOOLBOX_IMAGE=krops-toolbox:dev \
scripts/toolbox-run.sh teardown local-host
```

It could not reach the internal management API and stopped safely without
deleting the management cluster:

```text
cannot confirm the state of CAPD workload cluster 'local-workload';
leaving the management cluster intact
```

The teardown toolbox was then attached to the Docker `kind` network:

```bash
DOCKER_CONTEXT=default docker run --rm -it \
  --network kind \
  -v "$PWD:/workspace" \
  -w /workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/.kube:/root/.kube" \
  -e KUBECONFIG=/workspace/.kube/kind.yaml \
  krops-toolbox:dev \
  teardown local-host
```

The controlled teardown completed in order:

```text
Delete local-workload through Cluster API
        ↓
Remove local-management containers
        ↓
Remove krops-registry
```

Final output confirmed:

```text
CAPD workload cluster 'local-workload' deleted
management cluster containers removed
registry container 'krops-registry' removed
Teardown complete
```

The repository change to `replicaCount: 2` remains in the working tree. A
teardown removes runtime resources; it does not revert Git files.

## Problems and fixes

| Problem | Fix |
| --- | --- |
| Docker socket mount failed | Use `DOCKER_CONTEXT=default`. |
| Toolbox used old Mise | Build `krops-toolbox:dev`. |
| Host `kubectl` timed out | Use the published localhost API port. |
| Bare `flux` was missing | Run it through `mise exec`. |
| Python could not import `yaml` | Source the uv environment through Mise. |
| Multi-resource watch failed | Watch only `pods`. |
| Wrapper teardown could not connect | Join the Docker `kind` network. |

## Key takeaways

- The toolbox automates bootstrap and pivot. It is not a permanent controller.
- Management Flux manages infrastructure configuration.
- Workload Flux manages application configuration.
- Local Flux reads an OCI artifact; AWS Flux reads the GitHub `main` branch.
- `oci-push` is the local equivalent of publishing a configuration change.
- Flux applies desired state; Kubernetes and Helm perform the actual rollout.
- Persistent changes belong in repository YAML, not direct `kubectl` edits.
- Teardown uses safety checks and refuses destructive cleanup when it cannot
  confirm the workload cluster state.
