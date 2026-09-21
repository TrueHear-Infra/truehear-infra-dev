# TrueHear applications

`workload/base/` mirrors the `dev` environment of
[TrueHear/truehear-cloud-development](https://github.com/TrueHear/truehear-cloud-development)
(`kubernetes/environments/dev`, upstream commit `a68d852`):

| Component | Upstream overlay | What renders |
|---|---|---|
| `truehear-platform/` | `dev/{backend,vault,rabbitmq,redis}` | Namespaces + ServiceAccounts, `truehear.io/environment` label |
| `keycloak/` | `dev/keycloak` | Namespace, SAs, PostgreSQL 17.6 StatefulSet (TLS, scram), Keycloak 26.7.3 Deployment (2 replicas, HTTPS), Services, PDB |

`services/<svc>/` directories are byte-identical to upstream
`kubernetes/services/<svc>/` so `diff -r` against upstream shows only
krops-side files.

## Per-cluster values (`cluster-vars` ConfigMap in `flux-system`)

| Variable | Default | Upstream dev value |
|---|---|---|
| `TRUEHEAR_ENVIRONMENT` | `dev` | `dev` |
| `KEYCLOAK_HOSTNAME` | `https://keycloak.keycloak.svc.cluster.local` | `https://auth.dev.truehearkiosk.com` |
| `KEYCLOAK_STORAGE_CLASS` | `local-path` | `truehear-encrypted-gp3` |

Set them in the per-region FluxInstance ConfigMap under
`mgmt/<env>/addons/flux-apps/flux-instance.yaml` (the same ConfigMap that
carries `AWS_REGION` etc.).

## Not ported (follow-ups for the fork)

- `dev/keycloak/ingress.yaml`: AWS ALB Ingress with account-specific ACM
  certificate ARN and subnet IDs; needs the AWS Load Balancer Controller,
  which no krops cluster installs.
- Vault, RabbitMQ, Redis workloads: installed by hand upstream
  (`kubernetes/services/<svc>/doc/`), not by the dev overlays.
- The `truehear-encrypted-gp3` StorageClass (EBS CSI) on EKS.
- TLS: the committed `keycloak-tls` / `keycloak-postgresql-tls` Secrets come
  from a throwaway CA (`mise run keycloak-secrets`); upstream issues them
  from its `pki/` tree. Replace before any non-demo use.

## Secrets

Generated once with `mise run keycloak-secrets` (`--force` to rotate) and
committed SOPS-encrypted. Admin password:
`mise run sops-decrypt workload/base/keycloak/keycloak-bootstrap-admin.sops.yaml`.

## local-host demo

```sh
mise -E local-host run kubeconfigs
KUBECONFIG=local-workload.kubeconfig kubectl -n keycloak get pods
mise -E local-host run keycloak-port-forward     # https://localhost:8443/admin/
```
