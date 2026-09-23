# Dependencies

Dependency versions live in the files that consume them: mise configuration,
`bootstrap.toml`, Cargo manifests and locks, the toolbox Dockerfile,
Kubernetes and Flux manifests, and GitHub Actions workflows. There is no
central version catalog.

Renovate discovers the pins through [`renovate.json5`](../renovate.json5) and
opens weekly update PRs through the hosted Renovate GitHub App. Pending and
proposed updates appear in the Renovate dependency dashboard issue.

## Managed surfaces

Renovate discovers and updates versions in:

- `mise.toml`, `mise.aws.toml`, and `mise.local-host.toml`: tool pins.
  Explicit per-tool
  custom managers replace the native mise manager so each pin resolves against
  the intended upstream project.
- `pyproject.toml` and `uv.lock`: documentation site dependencies (mkdocs-material,
  pytest) through the native Python managers.
- `bootstrap-rs/Cargo.toml` and `bootstrap-rs/Cargo.lock`: Rust crate
  dependencies through Renovate's Cargo manager.
- `bootstrap.toml`: the Flux Operator, cert-manager, and CAPI Operator chart
  pins consumed by `krops-bootstrap`. One annotation-driven custom manager reads
  the adjacent `# renovate:` metadata. `mise run validate` cross-checks these
  pins against their declarative Helm releases and proxies. cert-manager's
  pin and its `pivot.sh`/HelmRelease counterparts share the `platform-charts`
  group so they can't drift apart (issue #322).
- `bootstrap-rs/Dockerfile`: digest-pinned build and runtime base images, and
  the mise CLI and Podman remote-client build arguments used by the toolbox.
  The `mise install` layer names tools without versions (`python`, `uv`,
  etc.), so every pin resolves from the copied `mise.toml` at build time;
  there is no inline version to keep in lockstep.
- `mgmt/**` and `workload/**` YAML: Flux, Helm, Kubernetes manifests, chart
  values, and clusterctl provider CRs under `capi-providers/`.
- `kindest/node` image tags wherever they are referenced in management
  manifests.
- `.github/workflows/`: GitHub Actions references and the Renovate CLI pin used
  by the managed-pin coverage tests.
- `pivot.sh`: imperative cert-manager and CAPI Operator chart pins, retained
  and grouped with `bootstrap.toml` and the Git manifests until the native
  shell path retires.

Grouping rules keep GitHub Actions updates together (excluding workflow container
images and runners), Flux updates together, CAPI updates together, imperative
chart pins with their declarative counterparts, and every pin whose value is a
literal Kubernetes release version together. Renovate proposes one PR at the
newest available version for each dependency, rather than parallel major and
non-major update PRs. Base images in `bootstrap-rs/Dockerfile` are
digest-pinned while retaining readable tags. Nothing automerges.

The `kubernetes-version` group (#142) covers `kindest/node` (docker datasource:
the local-host node image and both Cluster `topology.version` pins) and
`kubernetes/kubernetes` (github-releases datasource: the `kubectl` pin in
`mise.toml`). Both carry a literal Kubernetes release version rather
than an independent versioning scheme, so a Kubernetes bump opens a single PR
across every file that tracks it. Other `registry.k8s.io`
images (the CAPI/kubeadm provider controllers) are unaffected: they're
matched by exact depName, not by registry host, and stay in the separate
`cluster-api` group. The kind CLI follows its own release cadence and is
intentionally excluded from this group.

A chart/image version split once left cert-manager in ImagePullBackOff; the
`platform-charts` group and `tests/test-cert-manager-version-consistency.py`
guard the remaining pins (issue #322).

The CAPI group spans both the `github-releases` release lookups and the
`docker`-datasource digest-pinned images those same
providers deploy (`registry.k8s.io/cluster-api*`,
`registry.k8s.io/cluster-api-helm/*`, `gcr.io/k8s-staging-cluster-api/*`), so
a CAPI version bump lands its releases and images in one PR instead of
two.

## Toolbox release version

The `krops-bootstrap` package version lives in `bootstrap-rs/Cargo.toml`. It is a
release version, not a dependency pin, so Renovate does not increment it. When
a `v*` tag is pushed, `.github/workflows/toolbox-release.yml` fails unless the
tag matches that package version, then publishes the multi-architecture
toolbox image, signs it, and attaches an SPDX SBOM attestation.

Lifecycle tool versions installed in the image come from `mise.toml` and
`mise.aws.toml`. The Dockerfile separately pins its Rust builder and Debian
runtime base images, plus the mise installer and Podman remote-client build
arguments. Renovate manages those base references and build arguments.

## Update procedure

1. Wait for a Renovate PR. For configuration troubleshooting, use the pinned
   local dry-run procedure in [AGENTS.md](../AGENTS.md) under "Editing
   renovate.json5".
2. Review the raw and rendered diffs. The `validate` workflow checks kustomize
   builds, managed-pin extraction coverage, and `bootstrap.toml` consistency.
   Also (#322) that cert-manager's chart
   version (`bootstrap.toml`) matches `pivot.sh`
   (`tests/test-cert-manager-version-consistency.py`) -- catches the same
   drift the `platform-charts` Renovate group prevents, regardless of how it
   happens.
   And (#322) that the pinned cert-manager
   version (`bootstrap.toml`) supports the pinned Kubernetes version
   (`tests/test-cert-manager-kubernetes-support.py`) whenever a Renovate PR
   changes either pin -- a daily scheduled deployment failed silently for two
   weeks when cert-manager v1.21.x (supports v1.33-v1.36) was left paired
   with a Renovate-bumped Kubernetes v1.37.0 before this existed. There is no
   structured/versioned feed for cert-manager's supported-version window, so
   the check fetches and parses the same Markdown source that renders
   [cert-manager's "Currently supported releases" page](https://cert-manager.io/docs/releases/),
   rather than a compatibility table hardcoded here that would go stale the
   same way. That's a known fragile workaround, not a real fix; see the note
   below.
3. For toolbox inputs, also require the `bootstrap-rs` workflow's Rust checks
   and container build/smoke job.
4. Merge manually.

cert-manager's Helm chart's `kubeVersion` field only encodes a floor
(`>= 1.22.0-0`), not the real supported ceiling, so Helm's own install-time
validation doesn't catch this either. Keeping that field in sync would let
both Helm and this check rely on the chart's own metadata instead of scraped
docs; upstream already has that filed
([cert-manager/cert-manager#4132](https://github.com/cert-manager/cert-manager/issues/4132),
reopened as [#9123](https://github.com/cert-manager/cert-manager/issues/9123)
after the original went stale unresolved). If that ships, replace this
check's Markdown scrape with a read of the chart's own `kubeVersion` for the
pinned release.

## Intentional differences

- EKS cluster versions are not Renovate-managed pins and upgrade independently
  of `kindest/node`.
- EKS addon versions (`*-eksbuild.*`) have no public registry datasource and
  are updated manually.
- `scripts/toolbox-run.sh` defaults `TOOLBOX_IMAGE` to the mutable
  `ghcr.io/polarsquad/krops-toolbox:latest`; Renovate does not manage this
  runtime default.
- The toolbox Dockerfile installs `docker-ce-cli` from Docker's apt repository
  without a package-version pin. Its client version intentionally follows that
  repository, while the base image, mise installer, and Podman client remain
  Renovate-managed.
- `*.sops.yaml` `version:` fields, Kubernetes `apiVersion` strings, Helm chart
  `appVersion` values, and `bootstrap-rs/Cargo.toml`'s package version are not
  dependency pins.
