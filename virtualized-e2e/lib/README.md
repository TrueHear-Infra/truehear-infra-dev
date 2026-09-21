# lib: shared virtualized e2e harness components

Everything in this directory is identical across clouds. The per-cloud arms
(`virtualized-e2e/<cloud>/`) carry only what differs: the interception
mechanism, controller patches, boot stubs, and sanitizer literal lists.

## wiremock/: WireMock manifest templates

`namespace.yaml.tmpl`, `deployment.yaml.tmpl`, `service.yaml.tmpl` describe
the in-cluster WireMock: HTTPS only on 8443 behind a ClusterIP Service on
443, stub mappings mounted as files (in-memory stubs die with the pod), TLS
from a throwaway CA generated per run.

Parameters (envsubst style, example values in `wiremock/example.env`):

| Parameter | Meaning |
|---|---|
| `WIREMOCK_NAMESPACE` | Namespace hosting the Deployment/Service |
| `WIREMOCK_KEYSTORE_SECRET` | Secret with the JKS keystore and its passwords (keys `wiremock.jks`, `keystore-password`, `key-manager-password`) |
| `WIREMOCK_STUBS_CONFIGMAP` | ConfigMap with stub mapping files, mounted at `/home/wiremock/mappings` |

Render with example parameters:

```sh
cd virtualized-e2e/lib/wiremock
set -a; . ./example.env; set +a
for f in namespace service deployment; do
  envsubst < "$f.yaml.tmpl" > "/tmp/wiremock-$f.yaml"
done
```

`kustomization.yaml` lists the three templates as the shared base every arm
composes (`resources: ["../../lib/wiremock"]`); placeholders flow through
the build as strings, so `kubectl kustomize` stays green without rendering.

The Service name is fixed to `wiremock`; endpoint overrides and certificate
SANs derive from it (`https://wiremock.<namespace>.svc`). The image is pinned
by digest and tracked by Renovate (annotation on the image line in
`deployment.yaml.tmpl`).

### TLS material contract (Phase 4 creates this per run; never committed)

- Throwaway root CA. Its `ca.crt` is distributed to controllers as a
  `wiremock-ca` ConfigMap per consuming namespace (volumes cannot cross
  namespaces) and mounted by the arm patches as `SSL_CERT_FILE=/certs/ca.crt`.
- Server certificate signed by that CA, packed as a JKS keystore built with
  the WireMock image's own keytool (a LibreSSL-produced PKCS12 fails to load
  in the WireMock JRE; Phase 0 finding). Both `--keystore-password` and
  `--key-manager-password` must be set explicitly: the key-manager default
  is the literal string `password` regardless of the keystore password.
- SANs on the server certificate: `wiremock.<namespace>.svc` and
  `wiremock.<namespace>.svc.cluster.local`, the wildcard forms of BOTH
  (`*.wiremock.<namespace>.svc` and `*.wiremock.<namespace>.svc.cluster.local`;
  S3-style virtual-hosted bucket addressing dials the unqualified host form),
  plus `localhost` for admin access over `kubectl port-forward`.

## scenario-schema.json

The Phase 3 scenario/state-machine JSON shape: one document per stateful
cloud API scenario (ordered request/response steps bound to a WireMock
Scenario state machine). Phase 3 authors content against it; nothing here
generates mappings yet.

## sanitize_recording.py

Phase 2 recording scrubber. Replaces cloud-specific literals (account IDs,
tokens, generated names) in every JSON string value of a recording with
stable placeholders. The literal lists are per cloud and live in the arm
directories; only the engine lives here. `--check` reports unsanitized
files without writing (the CI guard shape).

## assertions.py

Phase 5 assertion helpers, importable and CLI-runnable:

- `deployment-available <namespace> <name>`: `kubectl wait
  --for=condition=Available` for a controller or the WireMock Deployment.
- `unmatched`: fails unless `GET /__admin/requests/unmatched` on the
  WireMock admin API is empty (minus `--allow`-listed entries). Reaches the
  admin API over `kubectl port-forward` as `https://localhost:<port>`, which
  is why the server certificate carries a `localhost` SAN; verification
  still uses the throwaway CA (`--cacert`).
