# AWS arm: WireMock interception for CAPA and ACK

Reference arm of the virtualized e2e harness (issue #355). Mechanism
proven by the Phase 0 spike; the full evidence is
[docs/wiremock-e2e-spike-findings-aws.md](../../../docs/wiremock-e2e-spike-findings-aws.md).

## Chosen mechanism: environment endpoint override

Both controller families honor `AWS_ENDPOINT_URL`, so no network-layer
interception (HTTPS proxy, CoreDNS rewrite of `*.amazonaws.com`) is needed:

- CAPA v2.13.0: the global `AWS_ENDPOINT_URL` covers every service the
  reconcile path calls. The patch also sets the per-service variants
  (`AWS_ENDPOINT_URL_EC2`, `_ELB`, `_STS`, `_IAM`, `_SSM`,
  `_SECRETSMANAGER`, `_EKS`) so Phase 2 can split recordings per service.
- ACK S3 1.11.0 (chart): same, with `AWS_ENDPOINT_URL_S3` and
  `_STS` alongside the global variable. The RDS and IAM charts share the
  chart template and the patch shape; their per-service variables are
  `_RDS` and `_IAM`.

TLS trust comes from `SSL_CERT_FILE=/certs/ca.crt` pointing at the throwaway
CA, mounted as a `wiremock-ca` ConfigMap volume. Go's `crypto/x509` honors
`SSL_CERT_FILE`, which covers CAPA and every ACK controller (all distroless
Go binaries) without SDK involvement. The ConfigMap must exist in EACH
consuming namespace (`capa-system`, `ack-system`): volumes cannot cross
namespaces. The Phase 4 script creates the CA and distributes the
ConfigMaps; the keystore Secret contract for WireMock itself is documented
in [../../lib/README.md](../../lib/README.md).

## Files

- `kustomization.yaml`: composes the shared WireMock templates from
  `../../lib/wiremock/` with the AWS stub ConfigMap. Buildable with
  `kubectl kustomize` (template placeholders flow through as strings).
- `stubs-configmap.yaml`: the STS `GetCallerIdentity` boot stub. Every ACK
  controller calls STS at startup and treats failure as fatal (the spike
  watched the unpatched controller crash loop against real
  `sts.amazonaws.com`), so this static mapping must be present from the
  start, before any Phase 2 recordings exist. Stub mappings ship as files
  because in-memory stubs are wiped by any pod restart.
- `patches/`: one strategic merge patch per controller Deployment, applied
  with `kubectl patch --type strategic --patch-file` (exact commands in
  each file's header). Targets live outside this tree:
  - `capa-controller-manager.yaml`: `capa-controller-manager` in
    `capa-system` (clusterctl-installed), container `manager`.
  - `ack-s3-controller.yaml`: `ack-s3-controller-s3-chart` in `ack-system`,
    container `controller`. Deployment names are `<Flux HelmRelease
    name>-<chart name>` from `mgmt/aws/infrastructure/ack-controllers/`.
  - `ack-rds-controller.yaml`: `ack-rds-controller-rds-chart`, same shape.
  - `ack-iam-controller.yaml`: `ack-iam-controller-iam-chart`, same shape.

## Apply order (what Phase 4 will automate)

1. Generate the throwaway CA, server certificate, and JKS keystore; create
   the keystore Secret in `wiremock-system` and the `wiremock-ca` ConfigMap
   in `wiremock-system`, `capa-system`, and `ack-system`.
2. Render the shared templates (`../../lib/wiremock/example.env` carries the
   values this arm uses) and apply them with `stubs-configmap.yaml`.
3. Patch the four controller Deployments. Patch BEFORE waiting on
   availability: an unpatched ACK controller crash loops on real-STS auth,
   so `helm --wait` style readiness races the patch (spike caveat).
4. WireMock availability and, later, the Phase 5 assertions ride
   `../../lib/assertions.py`.

## Known gap deferred to Phase 2

Bucket-scoped S3 operations use virtual-hosted addressing
(`<bucket>.wiremock.wiremock-system.svc`), which needs a CoreDNS `template`
stanza resolving that wildcard to the Service ClusterIP (exact stanza in
the spike findings doc). It ships with the Phase 2 recordings when the
Bucket CRD is in scope; the wildcard SANs it needs are already required on
the server certificate by the lib template.
