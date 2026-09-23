# TrueHear environments

## Purpose and the mirror-prod rule

This repository runs the TrueHear workload application (Keycloak with its
PostgreSQL, Vault, Redis, RabbitMQ, and the backend service) on EKS clusters
in eu-north-1. There are two environment levels: `staging` and
`prod`. Staging is defined in this repository; prod has no defined
size yet (the placeholder lives in `workload/environments/prod/README.md`).

The upstream rule is "mirror prod, lesser hardware": every environment runs
the same service set and topology, and only the hardware (instance type,
node counts) and the environment-specific values (hostname, Secrets)
differ. Anything that changes the replica counts or the resources in
[What runs where](#what-runs-where) is a deviation from the rule.

## Layering

Each TrueHear workload cluster runs its own Flux instance, applied by the
management cluster's `flux-apps` (HelmChartProxy + ClusterResourceSets
selected by the `environment` label). The application is layered in three
directories, following the base-plus-overlays pattern of the
[flux2-kustomize-helm-example](https://github.com/fluxcd-community/flux2-kustomize-helm-example)
reference:

- `workload/base/`: environment-neutral service roots and vendored charts
  (`keycloak`, the `truehear-platform` service bases for backend, rabbitmq,
  redis and vault, the in-repo `redis/chart` and `rabbitmq/chart`, and
  `vault/values.yaml`). Vendored files are copies from the upstream project
  and are never edited in place.
- `workload/platform/`: the cluster platform services shared by all
  environments: the `truehear-encrypted-gp3` StorageClass and the AWS Load
  Balancer controller HelmRelease (chart 3.5.0).
- `workload/environments/<env>/`: one overlay per environment level. It
  composes `workload/base` and carries the per-environment values, the
  Keycloak ALB Ingress, the HelmReleases, and the SOPS-encrypted Secrets.
- `workload/eu-north-1-<env>/`: the sync root of one workload cluster. It
  holds only Flux Kustomizations, which point at `workload/platform` and
  `workload/environments/<env>` and substitute from the per-cluster
  `cluster-vars` ConfigMap.

The staging sync root declares six Flux Kustomizations, in order:
`platform`, `truehear-platform`, `keycloak`, `vault`, `redis`, `rabbitmq`
(the last four depend on `truehear-platform`). `tests/test-workload-overlays.py`
pins that order and the per-root invariants.

## Environment matrix

| Environment | Cluster name | Instance type | Min/max nodes | Keycloak hostname | Sync root | FluxInstance ConfigMap |
|---|---|---|---|---|---|---|
| staging | `eu-north-1-staging` | `t3.medium` | 3 / 4 | `auth.staging.truehearkiosk.com` | `workload/eu-north-1-staging` | `flux-instance-staging` |
| prod | undefined | undefined | undefined | undefined | undefined | undefined |

The cluster definitions live in `mgmt/aws/clusters/eu-north-1/<env>/cluster.yaml`
(CAPI `Cluster`, `AWSManagedControlPlane`, and `MachinePool`: three AZs
`eu-north-1a/b/c`, 20 GiB gp3 root disks, on-demand capacity, the EKS
add-on set including `eks-pod-identity-agent`). The ConfigMaps and the
matching ClusterResourceSets are in
`mgmt/aws/addons/flux-apps/flux-instance.yaml`, one pair per environment.

## What runs where

The full stack is the reference sizing. This table is the sizing
source of truth (it comes from the plan's sizing facts, which the workers
must not change):

| Component | Replicas | Requests (each) | Limits (each) | Placement constraint |
|---|---|---|---|---|
| Keycloak | 2 | 500m / 1Gi | 2 / 2Gi | PDB minAvailable 1 |
| keycloak-postgresql | 1 | 250m / 512Mi | 1 / 1Gi | PVC 20Gi truehear-encrypted-gp3 |
| Vault (chart 0.34.1, image hashicorp/vault:2.0.4) | 3 | 100m / 256Mi | 500m / 512Mi | required anti-affinity on `topology.kubernetes.io/zone` |
| Redis (in-repo chart 0.3.0, redis:8.2.9-bookworm) | 6 | 100m / 256Mi | - / 512Mi | in-chart anti-affinity; PVC 10Gi |
| RabbitMQ (in-repo chart 0.1.0, rabbitmq:4.2.9-management) | 3 | 250m / 512Mi | - / 1Gi | required anti-affinity on `kubernetes.io/hostname`; PVC 10Gi |
| ALB controller (chart 3.5.0) | 2 | chart defaults | chart defaults | - |

Sum of requests about 5 GiB / 3.1 vCPU; it fits 3 x t3.medium (4 GiB each,
about 3.3 GiB allocatable) with headroom for kube-system. The 3-node floor
is a hard requirement, not a cost choice: Vault requires one member per
zone and the RabbitMQ chart one member per node, so scaling below three
leaves those StatefulSets Pending. Prod is expected to use a larger
instance type on the same shape.

## Adding an environment

1. Cluster definition: create `mgmt/aws/clusters/eu-north-1/<env>/` with
   `cluster.yaml` (Cluster labels `fluxcd: enabled`, `region: eu-north-1`,
   `environment: <env>`; control plane and MachinePool per the existing
   environments), `kustomization.yaml` (namePrefix), and
   `capi-nameref.yaml`. Register it in
   `mgmt/aws/clusters/eu-north-1/kustomization.yaml` and add a
   `Kustomization` entry in `mgmt/aws/clusters/flux-ks.yaml`.
2. Pod Identity: copy `mgmt/aws/infrastructure/<existing-env>-pod-identity/`
   to `<env>-pod-identity/` and rename the roles and associations
   `truehear-<env>-ebs-csi` and
   `truehear-<env>-aws-load-balancer-controller` (the `clusterName` values
   become `default_eu-north-1-<env>-control-plane`). Register the new
   directory in `mgmt/aws/infrastructure/kustomization.yaml` and its
   `flux-ks.yaml`.
3. Flux instance: add the `flux-instance-<env>` ConfigMap (sync path
   `workload/eu-north-1-<env>`, the `cluster-vars` values for the
   environment, the placeholder `VPC_ID` and ACM ARN) and the matching
   ClusterResourceSet (selector `environment: <env>`) to
   `mgmt/aws/addons/flux-apps/flux-instance.yaml`.
4. Bootstrap targets: add the teardown target and the `truehear-<env>-*`
   roles to `bootstrap.toml` (`[environments.aws.teardown.aws-workloads]`
   and `global-iam-roles`) and to `teardown.sh` (`WORKLOAD_TARGETS`,
   `GLOBAL_IAM_ROLES`). `tests/test-bootstrap-config.py` cross-checks the
   two against each other.
5. Secrets: generate the SOPS-encrypted overlay Secrets for the new
   environment with `mise run truehear-env-secrets -- --env <env>` (see
   [Secrets](./secrets.md) for the key setup).
6. Overlay: create `workload/environments/<env>/` mirroring the staging
   overlay (keycloak, vault, redis, rabbitmq, backend), with the
   environment's hostname and its generated Secrets.
7. Sync root: create `workload/eu-north-1-<env>/` with the six Flux
   Kustomizations (same shape as the staging `flux-ks.yaml`, paths
   repointed at the new overlay).
8. Test expectations: `tests/test-workload-overlays.py` derives most
   invariants from the tree (sync-root discovery, the cluster-vars
   intersection), but keep `EXPECTED_KS` and `FLUX_ROOTS` in sync when the
   root set changes. Then `mise run validate`, commit, push.

## Post-bootstrap operator steps

After the first bootstrap of an environment, these steps are manual:

1. Commit the identifiers: set `VPC_ID` and
   `KEYCLOAK_ACM_CERTIFICATE_ARN` in the environment's `cluster-vars`
   ConfigMap in `mgmt/aws/addons/flux-apps/flux-instance.yaml` (the ALB
   controller needs the literal VPC ID, and the Keycloak ALB Ingress needs
   the ACM certificate ARN). See [AWS environment](./aws.md), "Commit the
   identifiers".
2. Vault: the HelmRelease is Ready when the StatefulSet has rolled out,
   but the Pods stay sealed until an operator initialises and unseals
   Vault. Follow the upstream Vault infrastructure setup manual in
   TrueHear/truehear-cloud-development `documentation/services/vault`
   (init, unseal, token handling, the Kubernetes TLS Secret).
3. Redis: the HelmRelease is Ready before the cluster is formed. Check
   that the `redis-cluster-create` Job in the `redis` namespace completed;
   the Job's completion is the second signal.
4. RabbitMQ: rotate the bootstrap user. The chart creates its initial
   admin from the `rabbitmq-bootstrap-auth` Secret; after first access,
   set a new admin password imperatively and commit the re-encrypted
   Secret so Git is the source of truth again.
5. Route 53: the Keycloak ALB Ingress is internet-facing. Create the
   Route 53 alias record (A/AAAA) for `${KEYCLOAK_HOSTNAME}` pointing at
   the ALB whose name the Ingress annotation pins
   (`truehear-<env>-keycloak`); the ALB's DNS name is in the Ingress
   status.

## Follow-ups

- external-dns: the Route 53 alias is created by hand today; an
  external-dns HelmRelease would manage it from the Ingress annotations.
- cluster-autoscaler: the MachinePools declare min 3 / max 4, but no
  autoscaler runs yet, so the max is never reached.
- Keycloak realm import: the overlay ships the admin bootstrap Secret and
  the configuration; importing the realm (clients, mappers) is an operator
  step.
- RabbitMQ OIDC: management-UI access through Keycloak brokered login.
- Vault auto-unseal: KMS auto-unseal instead of the operator unseal.
- Public endpoint CIDR restriction: the ALB and the EKS API endpoint are
  open today; restrict them to the operator CIDRs.
- Prod sizing: define the prod instance type and node count (the
  mirror-prod direction: a larger type on the same shape).
