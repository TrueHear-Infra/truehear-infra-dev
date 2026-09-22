# Local OCI artifact rules

This document is the checklist for adding or changing resources in the KROPS
`local-host` profile. Read it before adding a management addon, workload
application, secret, chart, or container image.

For the complete Vault example, see the
[local-host Vault runbook](vault-local-host.md).

## Table of contents

- [The simple rule](#the-simple-rule)
- [Normal local artifact flow](#normal-local-artifact-flow)
- [Management component checklist](#management-component-checklist)
- [Workload component checklist](#workload-component-checklist)
- [Secrets checklist](#secrets-checklist)
- [Container image and Helm chart checklist](#container-image-and-helm-chart-checklist)
- [Air-gap differences](#air-gap-differences)
- [Validation and publication](#validation-and-publication)
- [Verification](#verification)
- [Examples](#examples)
- [Common mistakes](#common-mistakes)

## The simple rule

Use the ownership boundary to decide where a component belongs:

```text
Creates or configures clusters and cluster-wide delivery
    → mgmt/local-host

Runs as an application inside local-workload
    → workload/local-host
```

The connected local artifact task copies both complete directories:

```text
mgmt/local-host
workload/local-host
```

Therefore, adding a normal connected-local component does not require editing
the `oci-push` shell task. It does require registering the component in the
correct Kustomize root.

The air-gap artifact is different. Its management tree is selective, so a new
management component must also be added to
`airgap/scripts/build-config-artifact.sh`.

## Normal local artifact flow

The command is:

```bash
mise -E local-host run oci-push
```

It performs this flow:

```text
Current Git checkout
    ├── mgmt/local-host
    └── workload/local-host
              ↓
Temporary artifact directory
              ↓
oci://localhost:5001/krops:latest
              ↓
New immutable OCI digest
              ↓
Management Flux and workload Flux reconcile that digest
```

The OCI artifact contains configuration files. It does not build container
images, package application source code, or make an unregistered component
active automatically.

## Management component checklist

Use `mgmt/local-host` when management Flux must create or deliver the resource.

Create this component layout:

```text
mgmt/local-host/<area>/<component>/
├── kustomization.yaml
├── flux-ks.yaml
└── <resource manifests>
```

Complete every applicable item:

1. Put the Kubernetes resources in the component directory.
2. List those resources in the component's `kustomization.yaml`.
3. Create a Flux `Kustomization` in `flux-ks.yaml`.
4. Set its `spec.path` to the component directory.
5. Add `dependsOn` entries for everything that must be Ready first.
6. Register the component's `flux-ks.yaml` in
   `mgmt/local-host/kustomization.yaml` or its correct parent root.
7. If another component consumes it, update that consumer's `dependsOn`.
8. If the resource must land in `local-workload`, use the repository's addon
   delivery pattern, such as a `ClusterResourceSet`.
9. Match the workload cluster labels exactly in the delivery selector.
10. Update `AGENTS.md` when the change affects repository structure or
    architecture.

The management cluster holding a `ConfigMap` does not mean the contained
resource runs there. A `ClusterResourceSet` can read that payload and apply it
to the selected workload cluster.

## Workload component checklist

Use `workload/local-host` when the resource should run inside
`local-workload`.

Create this component layout:

```text
workload/local-host/<component>/
├── kustomization.yaml
├── flux-ks.yaml
└── <resource manifests>
```

Complete every applicable item:

1. Create the namespace explicitly.
2. Add the application resources or Helm source and release.
3. List deployable resources in the component's `kustomization.yaml`.
4. Create a Flux `Kustomization` in `flux-ks.yaml`.
5. Point `spec.sourceRef` at the workload `OCIRepository` named
   `flux-system`.
6. Set `spec.path` to `./workload/local-host/<component>`.
7. Use `dependsOn` only for a real readiness dependency.
8. Add SOPS decryption settings if the component contains `*.sops.yaml`.
9. Register `<component>/flux-ks.yaml` in
   `workload/local-host/kustomization.yaml`.
10. Update `AGENTS.md` when the new component changes repository navigation or
    architecture.

Files copied into the OCI artifact are inert until a root Kustomization or a
child Flux Kustomization references them.

## Secrets checklist

Never commit plaintext credentials or private keys.

1. Name encrypted manifests `*.sops.yaml`.
2. Add or confirm the matching path rule in `.sops.yaml`.
3. Encrypt only `data` and `stringData` fields.
4. Keep the age public recipient in `.sops.yaml`.
5. Keep `age.agekey` gitignored and outside the OCI artifact.
6. Add this to the component's Flux Kustomization:

   ```yaml
   decryption:
     provider: sops
     secretRef:
       name: sops-age
   ```

7. Bootstrap `flux-system/sops-age` in the cluster whose Flux instance
   performs the decryption.
8. Confirm encryption before publishing:

   ```bash
   mise exec -- sops filestatus path/to/secret.sops.yaml
   ```

9. Never print decrypted content merely to verify it. Pipe it into a dry-run
   parser instead.

Management Flux and workload Flux are separate. A decryption key in the
management cluster is not automatically available to workload Flux.

## Container image and Helm chart checklist

For the connected local profile:

- pin chart versions;
- pin image tags and digests where the repository convention requires them;
- declare Helm repositories or OCI chart sources in Git;
- expect Kubernetes or Helm to pull the image at reconciliation time;
- do not add image-building logic to `oci-push`.

The configuration artifact and application images are separate objects:

```text
OCI configuration artifact → YAML consumed by Flux
Container image             → executable workload pulled by Kubernetes
Helm chart                  → templates fetched by Helm Controller
```

## Air-gap differences

The normal local task and air-gap builder must not be confused.

<!-- markdownlint-disable MD013 -->

| Concern                  | Connected local artifact | Air-gap artifact                            |
| ------------------------ | ------------------------ | ------------------------------------------- |
| Management tree          | Complete copy            | Selective copy                              |
| Workload tree            | Complete copy            | Complete copy with rewrites                 |
| Public registry access   | Available                | Unavailable                                 |
| New management component | Register in normal root  | Also update air-gap copy and generated root |
| New image or chart       | Pulled at runtime        | Must be included, staged, and pinned        |

<!-- markdownlint-enable MD013 -->

When a management component must work in the air-gap package, update all
applicable locations:

1. `airgap/scripts/build-config-artifact.sh` directory creation and copy list;
2. the generated air-gap management root in that script;
3. any generated `dependsOn` chain in that script;
4. `airgap/zarf.yaml` for required charts, images, or files;
5. `airgap/images.txt` for the image inventory;
6. preload or staging scripts when workload nodes need the image offline;
7. air-gap ownership and image tests.

The storage addon is the reference example. The normal artifact included it
automatically because `mgmt/local-host` is copied in full. The air-gap builder
needed explicit storage directory creation, file copies, root registration,
and dependency ordering.

For a workload application, the air-gap builder already copies the full
`workload/local-host` directory. The YAML arrives automatically, but every
chart and image it needs must still be available without public network
access. SOPS bootstrap keys must also be available to workload Flux.

## Validation and publication

Run focused builds first:

```bash
kubectl kustomize mgmt/local-host >/dev/null
kubectl kustomize workload/local-host >/dev/null
```

Build the new component directly when possible:

```bash
kubectl kustomize path/to/new/component >/dev/null
```

Run repository checks:

```bash
git diff --check
mise run validate
```

Do not dismiss a validation failure silently. Record whether it was caused by
the current change or by a known unrelated workspace problem.

Publish only after the relevant builds pass:

```bash
mise -E local-host run oci-push
```

Record the returned digest. `latest` is only the moving reference; the digest
proves which exact artifact Flux applied.

## Verification

For management changes:

```bash
KUBECONFIG="$PWD/.kube/krops-mgmt-host.yaml" \
mise exec -- flux get kustomizations --watch
```

Verify both the management delivery object and the resulting workload
resource when the component uses a `ClusterResourceSet`.

For workload changes:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
mise exec -- flux get all --all-namespaces
```

Then inspect the actual Kubernetes objects:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods --all-namespaces
```

Do not call a change deployed merely because the OCI push succeeded. Confirm
that Flux reports the new digest as Ready and that the intended object is
healthy in the correct cluster.

## Examples

### Storage addon

Storage belongs under `mgmt/local-host/addons/storage` because management owns
cluster-wide addon delivery. Its `ClusterResourceSet` installs the actual
StorageClass and provisioner in `local-workload`.

The ordering is:

```text
local-workload-cni
        ↓
local-workload-storage
        ↓
flux-apps
```

### Vault application

Vault belongs under `workload/local-host/vault` because its Pods and PVCs run
inside the workload cluster.

Its TLS manifest is SOPS-encrypted, so `vault/flux-ks.yaml` references the
workload cluster's `sops-age` Secret.

### Backend identity

The backend Namespace and ServiceAccount belong under
`workload/local-host/backend`. The Flux Kustomization depends on Vault so
future backend resources are not reconciled before the Vault component is
Ready.

The ServiceAccount is declarative Kubernetes state. Vault policies, auth
methods, and roles are Vault-internal state stored in Raft and are configured
through the Vault API.

## Common mistakes

### Editing a live Kubernetes object

Do not use `kubectl edit` for a persistent change. Edit the repository,
publish a new artifact, and let Flux reconcile it.

### Forgetting the root registration

A directory can exist in the OCI artifact while doing nothing. Register its
`flux-ks.yaml` in the correct root Kustomization.

### Updating the normal script for every component

Do not add individual normal local files to `mise.local-host.toml`. The normal
task already copies the complete management and workload trees.

### Forgetting the air-gap selective copy

A new management component can work in the connected test and still be absent
from the air-gap artifact. Update the selective builder when air-gap support is
required.

### Confusing cluster ownership

A manifest stored under `mgmt/local-host` may deliver resources to the
workload cluster. Verify where the final resource exists, not only where its
delivery definition is stored.

### Confusing the configuration artifact with images

Pushing `krops:latest` publishes YAML. It does not publish a backend image,
Vault image, or Helm chart.

### Putting the SOPS key in Git

Only the age public recipient belongs in `.sops.yaml`. The private
`age.agekey` stays gitignored and is bootstrapped directly into the relevant
Flux namespace.
