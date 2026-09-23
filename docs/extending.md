# Adding clusters and apps

## Adding a workload cluster

1. Create `mgmt/aws/clusters/eu-north-1/<env>/` with a `cluster.yaml`,
   `kustomization.yaml` (set `namePrefix`), and `capi-nameref.yaml` (so CAPI
   cross-references get the prefix applied; see the existing environments).
2. Label the `Cluster` with `fluxcd: enabled`, `region: eu-north-1`, **and**
   `environment: <env>`.
3. Register it in `mgmt/aws/clusters/eu-north-1/kustomization.yaml` and add a
   `Kustomization` entry in `mgmt/aws/clusters/flux-ks.yaml` with
   `dependsOn: [capa-system]`.
4. Copy `mgmt/aws/infrastructure/<existing-env>-pod-identity/` to
   `<env>-pod-identity/` and rename the roles and associations
   `truehear-<env>-ebs-csi` and `truehear-<env>-aws-load-balancer-controller`
   (the `clusterName` values become `default_eu-north-1-<env>-control-plane`);
   register the new directory in `mgmt/aws/infrastructure/kustomization.yaml`
   and its `flux-ks.yaml`.
5. In `mgmt/aws/addons/flux-apps/flux-instance.yaml`, add the
   `flux-instance-<env>` ConfigMap (sync path `workload/eu-north-1-<env>`,
   the `cluster-vars` values, the placeholder `VPC_ID` and ACM ARN) and the
   matching `ClusterResourceSet` (selector `environment: <env>`).
6. Add the environment's teardown target to `bootstrap.toml`
   (`[[environments.aws.teardown.aws-workloads]]`) and the
   `truehear-<env>-*` roles to `[teardown].global-iam-roles` (and to
   `teardown.sh`); `tests/test-bootstrap-config.py` cross-checks the two.
7. Generate the environment's SOPS Secrets:
   `mise run truehear-env-secrets -- --env <env>`.
8. Create `workload/environments/<env>/` (mirror the staging overlay) and the
   `workload/eu-north-1-<env>/` sync root (the six Flux Kustomizations).
9. Keep `tests/test-workload-overlays.py` in sync (`EXPECTED_KS`,
   `FLUX_ROOTS`), then run `mise run validate`, commit, and push.

## Adding apps to the workload clusters

The layering (see [TrueHear environments](./truehear-environments.md)):
`workload/base/` (vendored, never edited in place) ->
`workload/environments/<env>/` (overlay: values, Ingress, HelmReleases,
Secrets via `mise run truehear-env-secrets -- --env <env>`) ->
`workload/eu-north-1-<env>/` (sync root, Flux Kustomizations only).

To add an app to a TrueHear environment:

1. Vendor the neutral manifests or chart into `workload/base/<app>/`.
2. Add `workload/environments/<env>/<app>/`: kustomization, HelmRelease for
   charts, SOPS Secrets from the mise task.
3. Add a Flux `Kustomization` for it in the sync root's `flux-ks.yaml`
   (`dependsOn: [truehear-platform]`, sops decryption, health checks).
4. Extend the expectations in `tests/test-workload-overlays.py`.
5. Run `mise run validate`, commit, and push.

## Using other providers

The management cluster is not AWS-only. Providers are declared as CAPI
operator CRs (`operator.cluster.x-k8s.io/v1alpha2`) under
`mgmt/<environment>/capi-providers/` (for example `mgmt/aws/capi-providers/` for
AWS EKS, `mgmt/local-host/capi-providers/` for local Docker, and
`mgmt/local-talos/capi-providers/` for Talos and Tinkerbell), one directory per
provider namespace, and registered in that environment's `capi-providers/flux-ks.yaml`.
The operator resolves the well-known provider names (`aws`, `talos`,
`k0sproject-k0smotron`) from the same built-in registry `clusterctl` uses, so
a provider is just a typed CR with a pinned version:

```
mgmt/aws/capi-providers/<name>-system/
  namespace.yaml
  kustomization.yaml        # lists namespace.yaml + providers.yaml (+ secrets)
  providers.yaml            # the typed provider CR(s)
  <credentials>.sops.yaml   # optional cloud credentials, SOPS-encrypted
```

The Flux registration in `flux-ks.yaml` follows the existing entries:
`dependsOn: [capi-system]`, `decryption.provider: sops` when credentials ship
in Git, and a `healthChecks` entry naming one CRD the provider installs so
Flux waits for it.

Contract note: this repo pins CAPI core v1.14.0, which speaks the v1beta2
contract and still accepts v1beta1-contract providers until the v1beta1
removal (tentatively CAPI v1.16, April 2027). Prefer providers that already
speak v1beta2.

### AWS (CAPA): the reference wiring

CAPA is the provider this repo already runs; use it as the template:

- `mgmt/aws/capi-providers/capa-system/providers.yaml` declares
  `InfrastructureProvider aws` v2.13.0 with `configSecret: aws-credentials`
  and the EKS feature gates
  (`EKS=true,EKSEnableIAM=true,EKSAllowAddRoles=true,MachinePool=true`).
- `aws-credentials.sops.yaml` carries `AWS_B64ENCODED_CREDENTIALS`, produced
  by the `aws-credentials` task (toolbox run, see [docs/aws.md](./aws.md)) (rotation: [docs/secrets.md](./secrets.md)).
- Cluster definitions in `mgmt/aws/clusters/eu-north-1/<env>/` use
  `AWSManagedControlPlane` + `AWSManagedMachinePool` (EKS). The ACK
  controllers and the per-environment Pod Identity CRs run on the management
  cluster (`mgmt/aws/infrastructure/ack-controllers/` and
  `mgmt/aws/infrastructure/<env>-pod-identity/`; see
  [docs/aws-iam.md](./aws-iam.md)).

### Talos (CABPT + CACPPT)

Talos supplies the bootstrap and control plane providers only; pair it with
any infrastructure provider (CAPT, CAPA, ...) that supplies the
machines. The worked in-repo example is the `local-talos` environment:
`mgmt/local-talos/` pairs the Talos providers with Tinkerbell (CAPT) to
PXE-boot a bare-metal management machine. See the architecture diagram in
[docs/local-talos-infra.svg](local-talos-infra.svg).

1. Two directories, matching the upstream namespace conventions:

   ```yaml
   # mgmt/local-talos/capi-providers/cabpt-system/provider.yaml
   apiVersion: operator.cluster.x-k8s.io/v1alpha2
   kind: BootstrapProvider
   metadata:
     name: talos
     namespace: cabpt-system
   spec:
     version: "v0.7.6"
     fetchConfig:
       url: "https://github.com/sidero-community/cluster-api-bootstrap-provider-talos/releases"
   ```

   The control plane provider is the same shape
   (`cacppt-system/provider.yaml`: ControlPlaneProvider `talos` v0.6.4).
2. Set `fetchConfig.url` explicitly to the
   [sidero-community](https://github.com/sidero-community) releases: the CAPI
   operator's embedded clusterctl defaults resolve `talos` to the archived
   siderolabs org. The sidero-community line serves the v1beta2 contract
   (`cluster.x-k8s.io/v1beta2: v1beta1` in the released metadata), so the
   contract note above does not impose a migration deadline on it.
3. No cloud credentials: Talos machine secrets are generated per cluster by
   `TalosControlPlane` / `TalosConfig`. Always set `talosVersion` explicitly
   (e.g. `v1.14`) so a provider upgrade does not silently change the
   generated machine config.
4. In cluster definitions, swap `KubeadmControlPlane` for `TalosControlPlane`
   and `KubeadmConfigTemplate` for `TalosConfigTemplate`; the infrastructure
   templates stay whatever the paired infra provider supplies.

### k0smotron

k0smotron v2.1.0 is v1beta2-native and ships bootstrap, control plane, and
infrastructure providers under one name:

```yaml
# mgmt/aws/capi-providers/k0smotron-system/providers.yaml
apiVersion: operator.cluster.x-k8s.io/v1alpha2
kind: BootstrapProvider
metadata:
  name: k0sproject-k0smotron
  namespace: k0smotron-system
spec:
  version: "v2.1.0"
---
apiVersion: operator.cluster.x-k8s.io/v1alpha2
kind: ControlPlaneProvider
metadata:
  name: k0sproject-k0smotron
  namespace: k0smotron-system
spec:
  version: "v2.1.0"
---
apiVersion: operator.cluster.x-k8s.io/v1alpha2
kind: InfrastructureProvider
metadata:
  name: k0sproject-k0smotron
  namespace: k0smotron-system
spec:
  version: "v2.1.0"
```

- No cloud credentials are needed, and the cert-manager the repo already
  installs covers its webhooks.
- Hosted control planes: `K0smotronControlPlane` runs the child cluster's
  control plane as pods on the management cluster; workers come from any
  infra provider.
- Remote machines: `RemoteMachine` provisions k0s onto existing machines over
  SSH (bare metal, arbitrary VMs, anywhere with no CAPI cloud provider).
- Drop the `InfrastructureProvider` CR if you only want hosted control
  planes.

### After adding a provider

1. `mise run validate` builds every overlay, including the new directory.
2. Open the PR; konflate renders the blast radius (new CRDs, provider
   deployments) into the PR comment and the `konflate / Rendered Flux diff`
   check.
3. After merge, confirm the provider came up:
   `kubectl get pods -n <name>-system` and
   `kubectl get <kind>providers.operator.cluster.x-k8s.io -A` (e.g.
   `infrastructureproviders`).
