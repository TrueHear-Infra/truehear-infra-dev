# Flux readiness timeout during local bootstrap

This note records a timeout observed while bootstrapping the KROPS
`local-host` profile on 22 September 2026. It also proposes a separate,
configurable Flux readiness timeout.

## Summary

The bootstrap command reported a failure while waiting for the `FluxInstance`:

```text
error: timed out waiting for the condition on fluxinstances/flux
Error: 'kubectl wait fluxinstance/flux --namespace flux-system
--for=condition=Ready --timeout=10m' failed with exit status: 1
```

The Kubernetes configuration was valid. The timeout occurred because controller
images took longer than ten minutes to become available inside the temporary
kind cluster.

Kubernetes and Flux continued running after the bootstrap command exited. Once
the images finished downloading, reconciliation completed and both permanent
clusters became Ready.

## What happened

### First attempt

The Flux `source-controller` pod remained in `ContainerCreating`:

```text
source-controller   0/1   ContainerCreating
```

Its events showed that the kind node was still pulling the image from GHCR:

```text
Pulling image "ghcr.io/fluxcd/source-controller:v1.9.5@sha256:..."
```

Because `source-controller` was unavailable, Flux could not fetch the local OCI
artifact:

```text
source-controller unavailable
        ↓
OCIRepository has no artifact
        ↓
Kustomization cannot apply mgmt/local-host
        ↓
FluxInstance does not become Ready
```

The failed bootstrap cluster was removed before retrying.

### Second attempt

The second attempt reached the same ten-minute timeout, but the slow image was
now `helm-controller`.

The Kubernetes event recorded:

```text
Successfully pulled image "ghcr.io/fluxcd/helm-controller:v1.6.4@sha256:..."
in 7m43s (11m29s including waiting)
```

The image completed approximately 90 seconds after the bootstrap wait had
already expired.

Flux then recovered without manual changes:

```text
FluxInstance                  Ready=True
Flux controller pods         Running
Management Kustomizations    Ready=True
local-management             Available=True, Provisioned
local-workload               Available=True, Provisioned
```

Each permanent cluster had one Ready control-plane machine and one Ready worker
machine.

## Why the command failed while the clusters succeeded

The timeout stops only the waiting command. It does not stop Kubernetes or
cancel Flux reconciliation:

```text
Bootstrap waits for 10 minutes
        ↓
The wait times out and the command exits
        ↓
Kubernetes continues pulling images
        ↓
Flux controllers start
        ↓
Reconciliation continues
        ↓
The permanent clusters become Ready
```

Rerunning bootstrap is safe because the Rust bootstrap lifecycle is designed to
reuse a healthy temporary cluster and continue from the existing state.

## Docker and kind image caches

The host Docker daemon and the kind node do not share an image cache:

```text
Docker host image cache       Separate
kind node containerd cache    Separate
```

Running `docker pull` confirmed that GHCR and the image were available, but it
did not place that image inside the kind node.

An attempted `kind load docker-image` also failed. The import requested an
all-platform archive, but Docker's export referenced content that was not
present locally:

```text
ctr: content digest sha256:...: not found
```

That import failure was separate from the Flux configuration and from the
original slow image download.

## Existing timeout settings

The FluxInstance readiness wait is currently hard-coded to ten minutes in both
lifecycle implementations:

- `bootstrap-rs/src/main.rs`
- `bootstrap-common.sh`

The existing environment setting below controls a different phase:

```toml
mgmt-ready-timeout = "15m"
```

`mgmt-ready-timeout` waits for the permanent management cluster during pivot.
Increasing it would not affect the earlier FluxInstance timeout.

## Proposal

Introduce a separate configurable FluxInstance readiness timeout, with a
twenty-minute value for `local-host`:

```toml
[environments.local-host]
flux-instance-ready-timeout = "20m"
mgmt-ready-timeout = "15m"
```

The lifecycle would use it here:

```text
kubectl wait fluxinstance/flux \
  --namespace flux-system \
  --for=condition=Ready \
  --timeout=<flux-instance-ready-timeout>
```

Twenty minutes provides useful tolerance for slow Docker Desktop or GHCR image
downloads while still failing within a bounded period when Flux is genuinely
stuck.

## Acceptance criteria

The proposed change should meet these conditions:

1. The timeout is declared in repository configuration, not duplicated as a
   new literal.
2. The Rust lifecycle and shell fallback use the same effective timeout.
3. Existing environments retain an explicit or documented default.
4. A real reconciliation error still returns a non-zero exit status.
5. A slow but progressing image pull has enough time to complete.
6. Tests verify that the configured value reaches the generated `kubectl wait`
   command.

## Immediate operator action

When this timeout occurs, inspect the Flux pods before tearing down:

```bash
kubectl --context kind-mgmt get pods --namespace flux-system --output wide
```

If the controllers later become Ready, rerun the same bootstrap command. The
toolbox will reuse the existing state and continue toward pivot:

```bash
DOCKER_CONTEXT=default \
TOOLBOX_IMAGE=krops-toolbox:dev \
scripts/toolbox-run.sh bootstrap local-host
```
