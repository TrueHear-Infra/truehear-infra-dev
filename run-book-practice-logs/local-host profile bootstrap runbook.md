Source: `docs/operations.md`, README quickstart, `mise.local-host.toml`, `bootstrap.toml` on krops `main` (`2904a41`, 2026-09-21). No cloud account, no GitHub PAT, no SOPS age key: everything runs in local containers.

In one line: a local kind cluster runs Flux; Flux provisions a small Docker (CAPD) workload cluster (`local-workload`, 1 control plane + 1 worker) from a local OCI registry; a second Flux instance on that cluster delivers Podinfo. Bootstrap and the pivot into the self-managed management cluster happen in one command.

## Prerequisites

- A running Docker engine (or Podman 5.5+).
- mise installed (repo requires `min_version = "2026.9.12"`).
- A checkout of `polarsquad/krops`.

Note: the host mise lifecycle tasks (`mise run bootstrap`, `mise run teardown`) are retired. The lifecycle runs through the `krops-toolbox` container via `scripts/toolbox-run.sh`; only the helper tasks (kubeconfigs, oci-push, port-forward) stay on host mise.

## 1. Get the repo and the host tools

```sh
git clone https://github.com/polarsquad/krops.git
cd krops
mise install
```

`mise install` brings the pinned host tools the helper tasks need (kubectl 1.37.0, kind 0.33.0, helm 4.3.0, clusterctl 1.14.2, flux2 2.9.4).

No `.env` needed. `local-host` syncs from a local OCI registry and creates no GitHub or SOPS secrets, so no PAT, age key, or AWS credentials. If a `.env` exists in the checkout, the wrapper forwards it into the container, but this profile consumes none of those values.

## 2. Toolbox image

The wrapper defaults to the published image `ghcr.io/polarsquad/krops-toolbox:latest` (v0.2.2, both architectures, verified published 2026-09-22). Use it as-is, or build the current checkout when you want unreleased CLI changes:

```sh
docker build -f bootstrap-rs/Dockerfile -t krops-toolbox:dev .
export TOOLBOX_IMAGE=krops-toolbox:dev
```

If you build a dev image, prefix the wrapper calls in step 3 and 8 with `TOOLBOX_IMAGE=krops-toolbox:dev`.

## 3. Check for a kind cluster name collision (this machine)

`bootstrap.toml` names the bootstrap kind cluster `mgmt`. If a kind cluster called `mgmt` already exists on the machine (for example the biggs.dog local-talos cluster), the run will reuse it and delete it after the pivot.

```sh
kind get clusters
```

If `mgmt` is listed and it is not a krops cluster from a previous run, use the escape hatch at the end of this runbook (scratch `BOOTSTRAP_CONFIG` with a renamed cluster) instead of the wrapper. Otherwise continue.

## 4. Bootstrap

```sh
scripts/toolbox-run.sh bootstrap local-host
```

What it does, in order:

1. Creates the `mgmt` kind cluster and a local registry container (`krops-registry`, reachable on the host at `localhost:5001`).
2. Installs the Flux Operator.
3. Packages `mgmt/local-host/` and `workload/local-host/` as the `krops:latest` OCI artifact and pushes it to the local registry.
4. Installs a FluxInstance that syncs the artifact's `mgmt/local-host` path.
5. Flux installs CAPI core, kubeadm, and the Docker (CAPD) provider and creates `local-workload`.
6. The management cluster installs a second Flux Operator + FluxInstance on `local-workload`, which reconciles `workload/local-host/` (Podinfo).
7. Pivots: the CAPI inventory moves into the self-managed management cluster and the kind cluster is deleted.

Bootstrap does not return until both the management and workload root Kustomizations are Ready (15-minute waits each by default). All phases are rerun-safe: after a failure, fix the cause and re-run the same command; kind stays authoritative until the final deletion.

## 5. Verify the management side

```sh
export KUBECONFIG="$PWD/.kube/krops-mgmt.yaml"
kubectl get kustomizations -n flux-system
flux get kustomizations --watch
```

The management kubeconfig is written to `.kube/krops-mgmt.yaml` (context `krops-mgmt`) by the pivot.

## 6. Export the workload kubeconfig

```sh
mise -E local-host run kubeconfigs
```

Writes `local-workload.kubeconfig` in the repo root. It rewrites the API server address to `127.0.0.1` on the published load-balancer port, because CAPD records a container-network IP that macOS cannot route.

## 7. Verify the workload side

```sh
KUBECONFIG=local-workload.kubeconfig kubectl get nodes
KUBECONFIG=local-workload.kubeconfig flux get all --all-namespaces
```

Expect 2 Ready nodes (1 control plane + 1 worker) and the Podinfo HelmRelease Ready.

## 8. Open Podinfo

```sh
mise -E local-host run podinfo-port-forward
```

Browse to <http://localhost:9898>. Press Ctrl-C to stop forwarding.

## Day-2: change config, republish

There is no Git sync here; the workload Flux watches the OCI artifact, so republish after edits:

```sh
mise -E local-host run oci-push
```

Optional overrides: `OCI_REPOSITORY=my-config OCI_TAG=v1 mise -E local-host run oci-push`. The artifact contains only `mgmt/local-host/` and `workload/local-host/`, so nothing else in the checkout gets packaged.

## Teardown

```sh
scripts/toolbox-run.sh teardown local-host
```

It suspends the workload Kustomization, deletes `local-workload` and waits for its containers to disappear, removes the management cluster containers, and removes `krops-registry` last.

## Pivot recovery

- Re-run the same command (`scripts/toolbox-run.sh pivot local-host` or bootstrap); the move is resumable and kind is only deleted at the end.
- `PIVOT_SKIP_DELETE=1` keeps the kind bootstrap cluster around for inspection after a completed pivot.
- Never delete moved CAPI `Cluster` objects on the target cluster to work around a failure: the provider treats that as deprovisioning and destroys the infrastructure. Re-run the move instead.

## Escape hatch: renamed kind cluster (scratch BOOTSTRAP_CONFIG)

Use this when a kind cluster named `mgmt` already exists on the machine. The wrapper does not forward `BOOTSTRAP_CONFIG`, so use a raw container run. The in-image CLI is `deny_unknown_fields`, so derive the scratch config from the checked-in `bootstrap.toml` (rename `kind-cluster`/`kind-context` only):

```sh
sed -e 's/^kind-cluster = .*/kind-cluster = "krops-local"/' \
    -e 's/^kind-context = .*/kind-context = "kind-krops-local"/' \
    bootstrap.toml > bootstrap.krops-local.toml

docker run --rm -it \
  -v "$PWD:/workspace" -w /workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/.kube:/root/.kube" \
  -e KUBECONFIG=/workspace/.kube/kind.yaml \
  -e BOOTSTRAP_CONFIG=bootstrap.krops-local.toml \
  ghcr.io/polarsquad/krops-toolbox:latest local-host
```

Teardown uses the same mounts with the `teardown` subcommand:

```sh
docker run --rm -it \
  -v "$PWD:/workspace" -w /workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/.kube:/root/.kube" \
  -e KUBECONFIG=/workspace/.kube/kind.yaml \
  -e BOOTSTRAP_CONFIG=bootstrap.krops-local.toml \
  ghcr.io/polarsquad/krops-toolbox:latest teardown local-host
```

Steps 5-8 (verification, kubeconfig export, Podinfo) are unchanged.
