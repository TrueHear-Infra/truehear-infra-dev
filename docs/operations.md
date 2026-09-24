# Operations

## Prerequisites

### Toolbox container (primary interface)

The toolbox image (`ghcr.io/polarsquad/krops-toolbox`) carries
`krops-bootstrap` plus every tool used by bootstrap, pivot, and teardown. It
intentionally omits development-only Go and Python toolchains.
The host needs the repository checkout and a running Docker engine or Podman
5.5+.

The toolbox image is published to `ghcr.io/polarsquad/krops-toolbox` for
Linux amd64 and arm64 as `X.Y.Z`, `X.Y`, and stable `latest`, signed with
GitHub OIDC and carrying a Syft SPDX JSON SBOM attestation. The
`toolbox-release` workflow publishes those tags from a matching `v*` tag.
Use a published tag, or build the current checkout only for unreleased
changes:

```sh
docker build -f bootstrap-rs/Dockerfile -t krops-toolbox:dev .
export TOOLBOX_IMAGE=krops-toolbox:dev
mkdir -p .kube
```

A complete raw Docker run for `local-host` is:

```sh
docker run --rm -it \
  -v "$PWD:/workspace" -w /workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/.kube:/root/.kube" \
  -e KUBECONFIG=/workspace/.kube/kind.yaml \
  "$TOOLBOX_IMAGE" local-host
```

This form assumes the standard `/var/run/docker.sock` daemon socket. Use the
wrapper below for Docker contexts or Podman installations with a different
host socket.

The AWS environment also needs the Git source, PAT, age key, and AWS
credentials inside the container. Source the repository `.env` so shell quotes
are removed, then pass values by name rather than putting secrets in argv:

```sh
cp .env.example .env
$EDITOR .env
set -a
. ./.env
set +a

docker run --rm -it \
  -v "$PWD:/workspace" -w /workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/.kube:/root/.kube" \
  -e KUBECONFIG=/workspace/.kube/kind.yaml \
  -e GIT_REPO_URL -e GITHUB_TOKEN -e GITHUB_USER \
  -e AGE_KEY_FILE -e AGE_PUBLIC_KEY \
  -e AWS_REGION -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
  -e AWS_SESSION_TOKEN -e AWS_PROFILE \
  "$TOOLBOX_IMAGE"
```

Generate the age key and re-encrypt secrets for a new fork before the AWS run;
see [Secret management](./secrets.md). If credentials come from an AWS shared
configuration instead of environment variables, also mount that configuration
under `/root/.aws` and pass `AWS_PROFILE`.

Podman socket locations differ across rootful Linux, rootless Linux, and
`podman machine`. Use the checked-in wrapper to resolve the host mount and the
socket path seen by the daemon:

```sh
TOOLBOX_IMAGE="$TOOLBOX_IMAGE" scripts/toolbox-run.sh bootstrap local-host
CONTAINER_ENGINE=podman TOOLBOX_IMAGE="$TOOLBOX_IMAGE" \
  scripts/toolbox-run.sh bootstrap local-host
```

The wrapper is the lifecycle entry point for every environment:

```sh
TOOLBOX_IMAGE="$TOOLBOX_IMAGE" scripts/toolbox-run.sh bootstrap aws
```

`KROPS_PROFILE` (or the positional profile argument after the lifecycle
verb, which reaches `krops-bootstrap`) selects the environment. It loads
every `.env` assignment with outer quote
stripping before detecting the engine or resolving a socket for it, so a
`.env`-selected `CONTAINER_ENGINE` takes effect from the start (issue #257).
An already-exported variable is left alone, so process environment wins over
`.env` for the wrapper. Note the opposite rule for the helper tasks: mise's
`env_file` loads `/workspace/.env` inside the container and its values
override the process environment, so a `-e NAME=value` on a helper run
loses to the same key in `.env` (see [Helper tasks in the
toolbox](#helper-tasks-in-the-toolbox)). It then passes only this
allowlist into the container:

- Engine and lifecycle: `CONTAINER_ENGINE`, `ENGINE_SOCK`, `KROPS_PROFILE`,
  `REGISTRY_PORT`, `OCI_REPOSITORY`, `OCI_TAG`, `BOOTSTRAP_PIVOT`,
  `PIVOT_SKIP_DELETE`
- GitHub and age: `GIT_REPO_URL`, `GITHUB_TOKEN`, `GITHUB_USER`, `AGE_KEY_FILE`,
  `AGE_PUBLIC_KEY`
- AWS: `AWS_REGION`, `AWS_PROFILE`, `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`

It does not pass `BOOTSTRAP_CONFIG`, `REGISTRY_READY_RETRIES`,
`LOCAL_RECONCILE_TIMEOUT`, `MGMT_KUBECONFIG`, `MGMT_READY_TIMEOUT`,
`MGMT_POLL_INTERVAL`, `BOOTSTRAP_KUBECONTEXT`, or the teardown controls
`AWS_ONLY`, `FORCE_KIND_DELETE`, `CLUSTER_DELETE_TIMEOUT`, and
`PROVIDER_DELETE_TIMEOUT`. Use a raw container run with explicit `-e` entries
when overriding those values.

Inside the toolbox:

- The entrypoint sets `KROPS_TOOLBOX=1` and execs `krops-bootstrap`, which
  owns engine detection, the daemon-side `ENGINE_SOCK` used by kind's socket
  mount, and kind-network attach/detach (issue #256).
- Bootstrap joins the `kind` network explicitly after creating (or reusing)
  the cluster; recreate, pivot, and teardown detach before deleting the
  bootstrap cluster; teardown re-joins first if it needs the internal API
  endpoint.
- Kind's internal API endpoint and `krops-registry:5000` then resolve by name.
- Host-only CAPD endpoint rewrites are skipped because the recorded endpoints
  already resolve on that network.
- On macOS the persisted `.kube/krops-mgmt.yaml` keeps the `kind` network
  address, which Docker Desktop does not route; see
  [Host-side access after a toolbox local-host run (macOS)](#host-side-access-after-a-toolbox-local-host-run-macos)
  for the host rewrite.
- `KUBECONFIG` must name one writable file. The documented invocation uses
  `/workspace/.kube/kind.yaml`, and the CLI replaces it with kind's internal
  kubeconfig after creation.
- `/root/.kube` maps to the checkout's `.kube/`, so the exported management
  kubeconfig persists on the host as `.kube/krops-mgmt.yaml`.

### Helper tasks in the toolbox

The one-off and helper steps (cloud preparation, SOPS key work, kubeconfig
exports, `oci-push`) are mise tasks defined in `mise.toml` and the
`mise.<env>.toml` layers. They run in the same toolbox image as the
lifecycle, by pointing the container's entrypoint at `mise` and running the
task from the mounted checkout. There is no wrapper verb for them; each
environment page shows its commands and they all follow one of two shapes.

Repo-only tasks (`sops-*`, `aws-credentials`) run as your own user so the
files they write are owned by you:

```sh
export TOOLBOX_IMAGE=ghcr.io/polarsquad/krops-toolbox:latest   # or krops-toolbox:dev
docker run --rm -it --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD:/workspace" -w /workspace \
  -e MISE_AUTO_INSTALL=0 \
  --entrypoint mise "$TOOLBOX_IMAGE" run <task> [args]
```

Cluster tasks (`mgmt-kubeconfig`, `kubeconfigs`, `oci-push`, the cloud
`*-bootstrap` and `*-federate` tasks) run as root, with the persisted
kubeconfig directory and the engine socket mounted like the lifecycle run:

```sh
docker run --rm -it \
  -v "$PWD:/workspace" -w /workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/.kube:/root/.kube" \
  -e KUBECONFIG=/workspace/.kube/krops-mgmt.yaml \
  -e MISE_AUTO_INSTALL=0 \
  --entrypoint mise "$TOOLBOX_IMAGE" -E <env> run <task>
```

Rules that apply to every helper run:

- `--entrypoint mise` bypasses `toolbox-entrypoint.sh`, so `KROPS_TOOLBOX`
  is not set. The local-host kubeconfig tasks need it (`-e KROPS_TOOLBOX=1`)
  together with `--network kind`: they then keep the CAPD-recorded
  kind-network endpoint instead of rewriting it to `127.0.0.1`, which inside
  a container is the container itself. A kubeconfig exported that way works
  from later toolbox runs on the kind network, not from host `kubectl`.
- `MISE_AUTO_INSTALL=0` is required. The mounted `mise.toml` pins dev-only
  tools (`go`) that the image does not carry; without the flag mise
  tries to install them, and as a non-root user that fails with `Permission
  denied` under `/usr/local/share/mise`.
- mise loads `/workspace/.env` (`env_file` in `mise.toml`) and its values
  override the process environment. Cloud credentials for `aws-bootstrap`
  and `aws-credentials` come from `.env`
  first; to run with other credentials, pass `-e MISE_ENV_FILE=/dev/null`
  and the variables by name (`-e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY
  -e AWS_SESSION_TOKEN -e AWS_REGION`). A bare `-e NAME` forwards the host
  value without placing it in argv.
- Podman: replace `docker` with `podman` and the socket source with the one
  `scripts/toolbox-run.sh` resolves (`podman info --format
  '{{.Host.RemoteSocket.Path}}'`).

Host-side on purpose: `mise run validate` (repository development, see
[Validation](#validation)) and `mise -E local-host run podinfo-port-forward`
(the browser is on the host; a toolbox form is shown with the local-host
chain below).

### Verifying a toolbox release

After the first release is published, replace `X.Y.Z` in these commands with
the matching Cargo and Git tag version:

```sh
IMAGE=ghcr.io/polarsquad/krops-toolbox:X.Y.Z
IDENTITY=https://github.com/polarsquad/krops/.github/workflows/toolbox-release.yml@refs/tags/vX.Y.Z

cosign verify \
  --certificate-identity "$IDENTITY" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  "$IMAGE"

cosign verify-attestation \
  --type spdxjson \
  --certificate-identity "$IDENTITY" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  "$IMAGE"
```

### AWS service quotas (common first-run blockers)

| Quota | Code | Needed | Why |
|---|---|---|---|
| EC2-VPC Elastic IPs (per region) | `L-0263D0A3` | ≥ 8 in `eu-north-1` (raised from the default 5) | One EIP per NAT gateway (3 AZs): two clusters in `eu-north-1` (management, staging) |
| VPCs per region | `L-F678F1CE` | 8 in `eu-north-1` (raised from the default 5) | One CAPA-created VPC per cluster plus pre-existing non-krops VPCs. e2e account 120392301094: `eu-north-1` quota raised to 8 |

The check is per region, and the default regional EIP limit (5) is below the
6 EIPs the default run creates, so a clean account stalls mid-run. Request the
increase before the first run with
`aws service-quotas request-service-quota-increase --service-code ec2 --quota-code <code> --desired-value <n> --region <region>`
(for VPCs use `--service-code vpc`).

### E2E account budget

The e2e AWS account 120392301094 carries a monthly cost budget
`krops-e2e-monthly` with a $200 ceiling. Notifications publish to the SNS
topic `krops-e2e-budget-alerts` (us-east-1) at 80% forecasted and 100%
actual spend, and the topic's email subscription delivers them to
joseph.shriner@polarsquad.com, the escalation path for budget alerts. The
email subscription only activates after the SNS confirmation email is
accepted.

## Configuration

Copy the env template and fill it in. Both the lifecycle wrapper
(`scripts/toolbox-run.sh`) and the helper mise tasks (through mise's `env_file`, inside the toolbox) load `.env`
automatically, and it is gitignored:

```sh
cp .env.example .env
$EDITOR .env
```

The Flux Operator chart is pulled anonymously for all environments. The
GitHub PAT is needed for the AWS environment (it syncs from GitHub); AWS
credentials and `AWS_REGION` are only needed with the AWS environment.

Repository-owned lifecycle configuration lives in `bootstrap.toml`. It defines
the environment names, sync paths, management clusters, imperative chart
versions, provider manifests, and teardown targets. `krops-bootstrap` reads it
from the working directory unless `BOOTSTRAP_CONFIG` selects another path.
Runtime environment variables take precedence over configurable defaults, and
`mise run validate` cross-checks the file against the manifests.

## Bootstrap

Every environment boots through the same lifecycle wrapper; the profile
selects the environment (the default is `aws` from `bootstrap.toml`):

```sh
TOOLBOX_IMAGE="$TOOLBOX_IMAGE" scripts/toolbox-run.sh bootstrap            # aws
TOOLBOX_IMAGE="$TOOLBOX_IMAGE" scripts/toolbox-run.sh bootstrap local-host
```

> Before the first AWS bootstrap, generate an age key for SOPS. See
> [Secret management](./secrets.md) for the host and toolbox-only setups.

This initial imperative phase performs these steps:

1. Creates the `mgmt` kind cluster.
2. Installs the Flux Operator (Helm).
3. Creates the `flux-github-pat` secret (for Git access), the `sops-age`
   secret (the age private key Flux uses to decrypt SOPS-encrypted secrets),
   and the `default/sops-age-resource-set` wrapper Secret, which the
   `flux-apps` ClusterResourceSets apply to each TrueHear workload cluster so
   its Flux can decrypt `workload/**/*.sops.yaml`.
4. Installs a `FluxInstance` that syncs `mgmt/aws/` and hands off to GitOps.
5. Pivots: moves the CAPI inventory into the self-managed management cluster
   and deletes the kind cluster (see [Pivot recovery](#pivot-recovery)).

Everything downstream (providers, the EKS clusters, the workload Flux
instances, the ACK controllers, and the per-environment Pod Identity roles
and associations) reconciles from Git with no further manual steps, apart from
the per-environment post-bootstrap steps in
[TrueHear environments](./truehear-environments.md).

The local-host environment performs the cluster, Flux Operator, and FluxInstance
steps in the `mgmt` management cluster, but does not create GitHub or SOPS
secrets. Instead, it bootstraps a local Docker Registry container (`registry:2`)
running on the host machine (accessible at `localhost:5001` by default),
publishes the `mgmt/local-host/` and `workload/local-host/` folders as the
initial `krops:latest` OCI
artifact, and configures Flux to reconcile that path from the artifact. Flux
then installs the CAPI core, kubeadm, and Docker infrastructure providers and
creates `local-workload`, a one-control-plane/one-worker Kubernetes cluster in
containers. The management cluster then installs a Flux Operator and
FluxInstance on `local-workload`; that instance reconciles
`workload/local-host/` from the same OCI artifact. CAPD is intended for local
development and testing, not production.

Together, these stages make `local-host` an end-to-end environment: one command
bootstraps the management control plane, publishes and reconciles the OCI
configuration, provisions a workload cluster through CAPI, installs a distinct
Flux control plane on that cluster, and reconciles a reachable Podinfo workload.
It exercises the complete cluster-to-workload GitOps lifecycle locally; only
the AWS-specific infrastructure and ACK resources are outside its scope.

**OCI Registry (local-host environment only):**
- Provides a local container registry for development workflows
- Enables developers to build and push OCI artifacts from git checkouts
- Flux syncs and deploys the OCI artifact without external dependencies
- Configurable via `REGISTRY_PORT` env var (defaults to 5001)
- Idempotent: restarts if stopped, no action needed if already running

**Workflow:**
```bash
# Republish the local management and workload folders after making changes
docker run --rm -it \
  -v "$PWD:/workspace" -w /workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/.kube:/root/.kube" \
  -e KUBECONFIG=/workspace/.kube/krops-mgmt.yaml \
  -e MISE_AUTO_INSTALL=0 --network kind -e REGISTRY_HOST=krops-registry -e REGISTRY_PORT=5000 \
  --entrypoint mise "$TOOLBOX_IMAGE" -E local-host run oci-push

# Optional overrides: add -e OCI_REPOSITORY=my-config -e OCI_TAG=v1 to the same run.

# FluxInstance pulls and reconciles the artifact's mgmt/local-host kustomization.
# The bootstrap configures kind's containerd to mirror localhost:5001 to the
# registry's in-cluster endpoint, krops-registry:5000, which is the endpoint
# the toolbox run above pushes to over the kind network.
```

The artifact contains only `mgmt/local-host/` and `workload/local-host/`,
preserving those directory paths when the artifact is pulled. Keeping the
source scope narrow also prevents local credentials and age private keys
elsewhere in the repository from being packaged.

The AWS environment adds the GitHub/SOPS secrets and configures the
FluxInstance to sync `mgmt/aws/`.

Watch reconciliation after a toolbox run with the persisted management
kubeconfig:

```sh
export KUBECONFIG="$PWD/.kube/krops-mgmt.yaml"
flux get kustomizations --watch
```

After a toolbox `local-host` run on macOS the persisted file carries the
`kind` Docker network address, which Docker Desktop does not route;
export the host copy from
[Host-side access after a toolbox local-host run (macOS)](#host-side-access-after-a-toolbox-local-host-run-macos)
instead.

For the local-host environment, export the CAPD workload kubeconfig after
`docker-workload-cluster` reports Ready. The toolbox run keeps the
kind-network endpoint, so the file is read back through the toolbox too:

```sh
docker run --rm -it \
  -v "$PWD:/workspace" -w /workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/.kube:/root/.kube" \
  -e KUBECONFIG=/workspace/.kube/krops-mgmt.yaml \
  -e MISE_AUTO_INSTALL=0 --network kind -e KROPS_TOOLBOX=1 \
  --entrypoint mise "$TOOLBOX_IMAGE" -E local-host run kubeconfigs
docker run --rm --network kind -v "$PWD:/workspace" -w /workspace \
  --entrypoint kubectl "$TOOLBOX_IMAGE" --kubeconfig local-workload.kubeconfig get nodes
```

The host task form of `kubeconfigs` still reads the persisted management
kubeconfig before it can export the workload one, so on macOS it must run
against the host copy from
[Host-side access after a toolbox local-host run (macOS)](#host-side-access-after-a-toolbox-local-host-run-macos).

The local workload Flux instance installs Podinfo from its OCI Helm chart.
Open it in a host browser by running the port-forward in a separate
terminal. This is the one helper that stays a host task on purpose (the
browser is on the host); it needs a host `kubectl` and a kubeconfig with the
`127.0.0.1` endpoint, which `mise -E local-host run kubeconfigs` on the host
produces:

```sh
mise -E local-host run podinfo-port-forward
```

On an engine-only host, publish the port from a toolbox run on the kind
network instead:

```sh
docker run --rm -it --network kind -p 9898:9898 \
  -v "$PWD:/workspace" -w /workspace -e MISE_AUTO_INSTALL=0 \
  --entrypoint kubectl "$TOOLBOX_IMAGE" --kubeconfig local-workload.kubeconfig \
  port-forward --namespace podinfo --address 0.0.0.0 service/podinfo 9898:9898
```

The workload uses Kubernetes v1.36.4. A CAPI ClusterResourceSet installs a
pinned Kindnet daemon as its CNI before the Flux addons are delivered.
The management cluster needs access to the container-engine socket, which
the bootstrap mounts automatically.

Local-host bootstrap first waits for the management `flux-apps` Kustomization
without printing transient `Unknown` status rows. After `flux-apps` becomes
Ready, it connects to `local-workload`, streams the workload Flux controller
error logs, and returns after the workload root Kustomization becomes Ready.
Filtering the workload stream to errors avoids showing normal startup retries
and advisory messages as apparent failures. Each readiness wait defaults to 15
minutes and can be changed with `LOCAL_RECONCILE_TIMEOUT` in a raw container
or fallback native run. The current wrapper does not forward that override.

EKS clusters typically take 15–25 minutes to come up; node groups and the
downstream app chain follow a few minutes after.

### Host-side access after a toolbox local-host run (macOS)

The pivot exports the management kubeconfig with the API server address CAPD
recorded: the `local-management-lb` container IP on the `kind` Docker
network. Native local-host runs rewrite that address to the localhost port,
but toolbox runs skip the rewrite (`should_rewrite_capd_endpoint` in
`bootstrap-rs/src/main.rs`) because the address resolves on the `kind`
network the toolbox joins. Docker Desktop on macOS runs the engine in a VM
and does not route the `kind` network from the host, so host commands
against the persisted `.kube/krops-mgmt.yaml` time out. Rewrite a host copy
to the port kind publishes on localhost:

```sh
cp .kube/krops-mgmt.yaml .kube/krops-mgmt.host.yaml
PORT=$(docker port local-management-lb 6443/tcp | head -1 | sed 's/.*://')
kubectl config set-cluster local-management --server="https://127.0.0.1:${PORT}" \
  --kubeconfig .kube/krops-mgmt.host.yaml
```

Use the host copy for every host command that talks to the management
cluster:

```sh
export KUBECONFIG="$PWD/.kube/krops-mgmt.host.yaml"
flux get kustomizations --watch
```

The host form of the `kubeconfigs` task reads the management kubeconfig
before it can export the workload one, so run it with the host copy
exported; without `KROPS_TOOLBOX` it rewrites the workload endpoint to
`127.0.0.1`, and `podinfo-port-forward` then needs no further changes:

```sh
mise -E local-host run podinfo-port-forward
```

The published port belongs to the `local-management-lb` container and stays
the same while that container exists. It changes when the container is
recreated, for example after teardown plus bootstrap. Re-run the
`docker port` and `kubectl config set-cluster` lines whenever
`docker port local-management-lb 6443/tcp` reports a different port than the
kubeconfig carries.

With Podman on macOS the same limitation applies inside the podman machine
VM; use `podman port` in place of `docker port`.

On Linux the host routes to the `kind` network directly, so
`.kube/krops-mgmt.yaml` works as-is and no rewrite is needed.

The unaffected alternative is a one-off toolbox container attached to the
`kind` network, where the recorded address resolves with no rewrite:

```sh
docker run --rm -it --network kind \
  -v "$PWD/.kube:/root/.kube" \
  -e KUBECONFIG=/root/.kube/krops-mgmt.yaml \
  --entrypoint flux \
  "$TOOLBOX_IMAGE" get kustomizations --watch
```

Use `--entrypoint kubectl` for kubectl commands. The `mise` helper tasks are
host forms; run them against the rewritten copy.

Teardown is unaffected: it reads the management kubeconfig from inside the
toolbox, which is attached to the `kind` network.

### Verifying the full chain

For the local-host end-to-end chain:

```sh
TOOLBOX_IMAGE="$TOOLBOX_IMAGE" scripts/toolbox-run.sh bootstrap local-host
docker run --rm -it \
  -v "$PWD:/workspace" -w /workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/.kube:/root/.kube" \
  -e KUBECONFIG=/workspace/.kube/krops-mgmt.yaml \
  -e MISE_AUTO_INSTALL=0 --network kind -e KROPS_TOOLBOX=1 \
  --entrypoint mise "$TOOLBOX_IMAGE" -E local-host run kubeconfigs
docker run --rm --network kind -v "$PWD:/workspace" -w /workspace \
  --entrypoint flux "$TOOLBOX_IMAGE" --kubeconfig local-workload.kubeconfig get all --all-namespaces
# then the port-forward from the section above, and browse to http://localhost:9898
```

The toolbox form above needs no rewrite: the runs stay on the `kind`
network. Running those steps as host commands instead needs the host copy
from
[Host-side access after a toolbox local-host run (macOS)](#host-side-access-after-a-toolbox-local-host-run-macos)
in place of `.kube/krops-mgmt.yaml`.

Bootstrap does not return until the management and workload root
Kustomizations are Ready. The final port-forward verifies that the workload
Flux instance successfully delivered the application.

For the AWS chain:

```sh
# Management cluster after a toolbox run
export KUBECONFIG="$PWD/.kube/krops-mgmt.yaml"
kubectl get kustomizations -n flux-system            # all Ready (incl. ack-controllers, staging-pod-identity)
kubectl get clusters.cluster.x-k8s.io -A             # eu-north-1-management, eu-north-1-staging Provisioned
kubectl get roles.iam.services.k8s.aws -n ack-system # truehear-staging-*, krops-reader
kubectl -n ack-system get podidentityassociations.eks.services.k8s.aws

# Workload clusters: export the kubeconfigs directly (the TrueHear cluster
# names do not match the `kubeconfigs` task's krops naming), then
#   aws eks update-kubeconfig --name default_eu-north-1-staging-control-plane --region eu-north-1 --kubeconfig .kube/krops-workloads.yaml --alias eu-north-1-staging
#   export KUBECONFIG=.kube/krops-workloads.yaml
#   kubectl config use-context eu-north-1-staging
kubectl get kustomizations -n flux-system            # platform, truehear-platform, keycloak, vault, redis, rabbitmq (staging)
```

There are deliberately no `buckets`/`dbinstances` resources: the S3 and RDS
ACK controllers and the `workload-resources/` example CRs were removed, so
`kubectl get buckets.s3.services.k8s.aws` fails with no CRD.

## Pivot recovery

Bootstrap ends with a pivot: the CAPI inventory moves from the local `mgmt`
kind cluster into the self-managed management cluster, and the kind cluster
is deleted. `scripts/toolbox-run.sh bootstrap` runs the pivot by default
(`BOOTSTRAP_PIVOT=0` opts out). `scripts/toolbox-run.sh pivot` starts the same rerun-safe CLI
and resumes through the pivot; there is no separate pivot subcommand. See
[the bootstrap CLI](./bootstrap-cli.md) for its interface and controls.

`clusterctl move` is re-runnable: an object is deleted from the source kind
cluster only after it was created on the target, so kind stays authoritative
until the final kind deletion. On a clean first run the pivot also WAITS for
two Flux-driven prerequisites instead of failing fast: the management
`Cluster` definition (polled up to `MGMT_READY_TIMEOUT`, surfacing failed
Kustomizations on timeout) and the first target nodes (up to 15m, tolerating
a nodeless EKS start). A timeout in either prints the Kustomization or node
state and is safe to re-run. If a pivot phase fails:

1. Fix the reported cause.
2. Re-run the pivot (`scripts/toolbox-run.sh pivot`, or rerun bootstrap)
   from a checkout of the revision you want self-managed, normally `main`. A
   fallback native run must use the bootstrap context (`kind-mgmt`;
   `BOOTSTRAP_KUBECONTEXT` overrides). Toolbox mode selects kind's internal
   kubeconfig automatically. The CLI reuses an existing healthy `mgmt` kind
   cluster on rerun.
3. Set `PIVOT_SKIP_DELETE=1` to keep the kind bootstrap cluster around for
   inspection once the pivot completes.

> Note: never delete moved `Cluster`, `AWSManaged*`, `MachinePool`, or
> `Dev*` objects on the target cluster to work around a failure. The CAPI
> providers treat deletion as deprovisioning and destroy the real
> infrastructure (EKS clusters, VPCs, IAM roles; CAPD the local containers).
> Re-run the move instead. Only pure-config duplicates without a move hook
> (for example an identity created by hand during a failed install) are safe
> to delete.

The management kubeconfig is written to `MGMT_KUBECONFIG`, with context
`krops-mgmt`. The toolbox mount makes its `/root/.kube/krops-mgmt.yaml`
appear on the host as `./.kube/krops-mgmt.yaml`; a fallback native run
defaults to `~/.kube/krops-mgmt.yaml`. After a toolbox `local-host` run on
macOS the file carries the `kind` Docker network address; see
[Host-side access after a toolbox local-host run (macOS)](#host-side-access-after-a-toolbox-local-host-run-macos)
for host-side use.

## Teardown

The wrapper runs the Rust subcommand in the toolbox:

```sh
TOOLBOX_IMAGE="$TOOLBOX_IMAGE" scripts/toolbox-run.sh teardown            # aws
TOOLBOX_IMAGE="$TOOLBOX_IMAGE" scripts/toolbox-run.sh teardown local-host
```

The retained `./teardown.sh` reference path and a native
`krops-bootstrap teardown [PROFILE]` run are the fallbacks. The positional
profile selects an environment from `bootstrap.toml`. Teardown reads
resource names and targets from `bootstrap.toml` and discovers where the
CAPI controllers are running:

- the `mgmt` kind cluster before pivot
- the exported self-managed management kubeconfig after pivot
- no reachable Kubernetes controller host, which falls back to AWS orphan
  cleanup for the AWS environment

The main controls keep the shell interface:

| Variable | Default | Effect |
|---|---|---|
| `AWS_ONLY` | `0` | Literal `1` runs only the AWS orphan sweep; invalid with `local-host` |
| `FORCE_KIND_DELETE` | `0` | Literal `1` overrides the final controller-host deletion guard |
| `CLUSTER_DELETE_TIMEOUT` | `1200` seconds | CAPI cluster deletion wait (aws workloads) |
| `PROVIDER_DELETE_TIMEOUT` | `300` seconds | CAPI provider deletion wait |
| `MGMT_KUBECONFIG` | `~/.kube/krops-mgmt.yaml` (in the toolbox that is the checkout's `.kube/` mount) | Post-pivot controller-host kubeconfig |

The hard preflight depends on the mode:

| Mode | Required tools |
|---|---|
| `local-host` | `kind`, `kubectl`; `AWS_ONLY=1` is rejected |
| normal `aws` | `kind`, `helm`, `kubectl`, `xargs`; a missing AWS CLI skips the orphan sweep |
| `AWS_ONLY=1` | AWS CLI only |

A required-tool failure happens before mutation. The wrapper does not forward
the four teardown controls in the table above; use a raw container invocation
with explicit `-e` entries or a fallback native run for recovery overrides.

For `local-host`, teardown suspends the workload Kustomization, deletes the
CAPD workload cluster, waits for its containers to disappear, removes either
the pre-pivot kind cluster or the post-pivot self-managed management
containers, and removes `krops-registry` last.

For `aws`, teardown suspends Flux, deletes every workload CAPI Cluster (staging)
while leaving the management Cluster object alone, and waits
before touching the controller host. It then runs a best-effort AWS sweep per
environment-level target from `bootstrap.toml` (the
`eu-north-1-staging` workloads, plus the self-managed management cluster
itself). The sweep removes nodegroups, EKS control planes, CAPA-tagged VPC
resources in dependency order, the CAPA per-cluster IAM roles, the
`truehear-<env>-*` Pod Identity roles (with their attached and inline
policies), the `krops-reader` user, and the `clusterawsadm` CloudFormation
stack. The customer-managed ALB `Policy` resources are not deleted and must be
removed manually; there is no S3/RDS sweep because the repo declares none. It
removes CAPI providers and bootstrap Helm releases when the controller host
remains reachable.

The controller-host guard prevents removal while CAPI workload deletion is
unconfirmed. Do not bypass it unless you accept orphaned infrastructure. If a
management cluster is already unreachable, rerun with `AWS_ONLY=1` to make the
recovery mode explicit. A missing AWS CLI in normal AWS mode is reported and
the orphan sweep is skipped; `AWS_ONLY=1` requires the CLI and fails preflight
without it.

ACK resources can survive if their workload cluster disappears before their
custom resources finish deleting. The explicit sweep is what removes those
orphans, including both workload environments and the self-managed management
cluster.

## Validation

Run the repository validation before pushing:

```sh
mise run validate
```

The task checks shell syntax for the retained lifecycle scripts, runs the
`bootstrap.toml` manifest cross-check and the toolbox/doc tests, and builds
every kustomize overlay under `mgmt/`, `workload/`, and `virtualized-e2e/`.
It is repository development, so it stays a host task on purpose (it needs
the pinned Python, `uv`, and `kubectl`). Contributors without a host
toolchain can run the same task in the toolbox, redirecting the uv
environment off the mount:

```sh
docker run --rm -v "$PWD:/workspace" -w /workspace \
  -e MISE_AUTO_INSTALL=0 -e UV_PROJECT_ENVIRONMENT=/tmp/krops-venv \
  --entrypoint mise "$TOOLBOX_IMAGE" run validate
```

`.github/workflows/validate.yml` separately builds every overlay, runs the
Renovate managed-pin coverage tests, cross-checks
`bootstrap.toml`, and lints YAML on pushes to `main` and on pull requests.
`.github/workflows/bootstrap-rs.yml` runs Rust format, clippy, build, and tests,
then builds and smokes the toolbox image when its inputs change.
