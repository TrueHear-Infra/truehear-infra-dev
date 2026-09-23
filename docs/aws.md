# AWS environment

The `aws` environment is the TrueHear reference: a disposable kind bootstrap
cluster runs Flux, CAPA v2.13.0 provisions self-managed EKS clusters, the
pivot moves the management objects into the `eu-north-1-management` cluster,
and the management cluster runs the ACK operators (IAM, EKS) reconciling the
per-workload-cluster Pod Identity roles, policies, and associations from
`mgmt/aws/infrastructure/<env>-pod-identity/`. There are no S3 buckets or RDS
instances in the account: the krops S3/RDS controllers and the
`Bucket`/`DBInstance`/reader-`Role` example CRs were removed (the
`workload-resources/` directory no longer exists). See
[TrueHear environments](./truehear-environments.md) for the environment
levels, sizing, and the per-environment operator steps.

![krops aws architecture](aws-infra.svg)

## Clusters

| Region | Clusters |
|---|---|
| `eu-north-1` | `eu-north-1-management` (the self-managed management cluster, provisioned by the pivot) and `eu-north-1-staging` |

Every cluster is an EKS control plane (EKS 1.34.6). The management cluster
runs one ARM (Graviton2) `AWSManagedMachinePool` at the cheapest offered
2 vCPU / 4 GiB shape (2 x t4g.medium, min 2 / max 3); the TrueHear workload
clusters run one x86 pool each (3 x t3.medium, min 3 / max 4, three AZs
`eu-north-1a/b/c`, 20 GiB gp3 root disks, on-demand, the EKS add-on set
including `eks-pod-identity-agent`). The management cluster lives in
`eu-north-1` and, after the pivot, reconciles its own Cluster objects.

## Prerequisites

- An AWS account where you hold permission to create EKS clusters, VPCs, and
  IAM roles. The static credentials principal runs every ACK controller on
  the management cluster, so it needs the Pod Identity plumbing scope for
  every TrueHear workload cluster: IAM role management scoped to
  `truehear-*` names, customer-managed policy management scoped to
  `truehear-*` names, the `krops-reader` console user actions,
  `eks:*PodIdentityAssociation*` on `default_eu-north-1-staging-control-plane`, and `iam:PassRole` to
  `pods.eks.amazonaws.com` on `truehear-*` roles. See
  [AWS authentication & IAM](./aws-iam.md) for the exact actions and the
  least-privilege trade-off.
- A GitHub PAT and an age key as for any GitHub-synced environment (`.env`,
  see [operations.md](./operations.md)). No host toolchain: `aws-cli` and
  `clusterawsadm` are in the toolbox image.
- The `clusterawsadm` IAM CloudFormation stack, provisioned once before
  bootstrap and removed by a full AWS teardown. `clusterawsadm` reads the
  AWS credentials from `.env` (mise `env_file`) or the process environment
  ([helper run rules](./operations.md#helper-tasks-in-the-toolbox)):

  ```sh
  docker run --rm -it -v "$PWD:/workspace" -w /workspace -e MISE_AUTO_INSTALL=0 \
    --entrypoint mise "$TOOLBOX_IMAGE" -E aws run aws-bootstrap
  # == clusterawsadm bootstrap iam create-cloudformation-stack --region eu-north-1
  ```

- AWS service quotas for a clean account. The default run creates two EKS
  clusters in `eu-north-1` (management and `eu-north-1-staging`), each with its
  own CAPA-created VPC and a NAT gateway per AZ (one EIP each): 4 EIPs and 2
  VPCs in the region. The default
  regional EIP limit is 5, so request the increase before the first run (see
  [Operations](./operations.md#aws-service-quotas-common-first-run-blockers)).

## Credentials

There are two credential surfaces, and neither is a credential on a workload
cluster.

- **CAPA (management cluster).** The EKS control planes and node pools are
  provisioned by CAPA from a single SOPS-encrypted profile in Git at
  `mgmt/aws/capi-providers/capa-system/aws-credentials.sops.yaml` (the
  `configSecret` on the `aws` infrastructure provider). To set or rotate it:

  ```sh
  krops_mise() {   # from docs/secrets.md
    docker run --rm -it --user "$(id -u):$(id -g)" -e HOME=/tmp \
      -v "$PWD:/workspace" -w /workspace \
      -e MISE_AUTO_INSTALL=0 -e SOPS_AGE_KEY_FILE=/workspace/age.agekey \
      --entrypoint mise "$TOOLBOX_IMAGE" "$@"
  }
  krops_mise -E aws run aws-credentials     # clusterawsadm bootstrap credentials encode-as-profile
  # paste the printed profile into
  #   mgmt/aws/capi-providers/capa-system/aws-credentials.sops.yaml
  #   as AWS_B64ENCODED_CREDENTIALS
  krops_mise run sops-encrypt mgmt/aws/capi-providers/capa-system/aws-credentials.sops.yaml
  ```

  (The CloudFormation stack from Prerequisites must already exist.)

- **ACK controllers (management cluster).** Same static SOPS credential
  pattern (`mgmt/aws/infrastructure/ack-controllers/aws-credentials.sops.yaml`).
  The IAM and EKS controllers run on the management cluster and reconcile the
  per-workload-cluster Pod Identity roles, policies, and associations declared
  in `mgmt/aws/infrastructure/<env>-pod-identity/` (staging). Workload
  clusters run no controllers and hold no credentials. The static principal's
  policy must cover the union of the former per-controller pod-identity roles;
  see [AWS authentication & IAM](./aws-iam.md) for the full action list and
  the least-privilege trade-off.

## Commit the identifiers

1. `mgmt/aws/addons/flux-apps/flux-instance.yaml` carries one `cluster-vars`
   ConfigMap per environment (the `flux-instance-staging`
   ConfigMap). After the first bootstrap of each
   environment, commit the real `VPC_ID` (the ALB controller cannot use
   IMDS with hop limit 1, so it needs the literal VPC ID) and the
   `KEYCLOAK_ACM_CERTIFICATE_ARN` (the ACM certificate the Keycloak ALB
   Ingress terminates on) in both ConfigMaps. Until then the placeholders
   `REPLACE_AFTER_FIRST_BOOTSTRAP` and `REPLACE_WITH_ACM_CERT_ARN` are
   committed.
2. The account ID is a literal in the per-environment `cluster-vars`
   ConfigMaps of `mgmt/aws/addons/flux-apps/flux-instance.yaml`
   (`AWS_ACCOUNT_ID`); there is no `cluster-vars` ConfigMap on the
   management cluster. The console reader user in
   `mgmt/aws/infrastructure/aws-global-iam/reader-user.yaml` can only
   `sts:AssumeRole` on `arn:aws:iam::*:role/truehear-*`.
3. The EKS version is pinned in each `cluster.yaml`
   (`mgmt/aws/clusters/<region>/<env>/`); bump it deliberately, not with a
   generic dependency update.

## Bootstrap, pivot, teardown

```sh
scripts/toolbox-run.sh bootstrap aws   # kind + Flux + CAPA; then pivot into eu-north-1-management
export KUBECONFIG="$PWD/.kube/krops-mgmt.yaml"   # written by the pivot, context krops-mgmt
docker run --rm -it \
  -v "$PWD:/workspace" -w /workspace \
  -v "$PWD/.kube:/root/.kube" \
  -e KUBECONFIG=/workspace/.kube/krops-mgmt.yaml -e KUBECONFIG_FILE=/root/.kube/krops-workloads.yaml \
  -e MISE_AUTO_INSTALL=0 \
  --entrypoint mise "$TOOLBOX_IMAGE" -E aws run kubeconfigs   # aws eks update-kubeconfig per region
export KUBECONFIG="$PWD/.kube/krops-workloads.yaml"
```

The workload kubeconfig lands in the checkout's `.kube/` through the
`/root/.kube` mount (`KUBECONFIG_FILE` overrides the task's `~/.kube`
default so the file persists on the host). Re-export the management
kubeconfig with `-E aws run mgmt-kubeconfig` in the same run shape only if
`.kube/krops-mgmt.yaml` was deleted; the pivot already wrote it.

Bootstrap ends with the pivot: the CAPI inventory moves from the disposable
`mgmt` kind cluster into the self-managed `eu-north-1-management` EKS cluster
and the kind cluster is deleted (see [Pivot recovery](./operations.md#pivot-recovery)).

Teardown is automated for `aws`. `scripts/toolbox-run.sh teardown aws`
suspends Flux, deletes every workload CAPI Cluster (staging), runs a
best-effort AWS orphan sweep per environment-level target from
`bootstrap.toml` (nodegroups, EKS control planes, CAPA-tagged VPC resources,
CAPA per-cluster IAM roles, the `truehear-<env>-*` Pod Identity roles, the
`krops-reader` user, and the `clusterawsadm` CloudFormation stack), removes
the self-managed management cluster through the same sweep, and removes the
kind bootstrap cluster. The sweep does not delete the customer-managed ALB
`Policy` resources, which must be removed manually. There is no S3/RDS sweep
(the repo declares none). See [Teardown](./operations.md#teardown) for the
controls.

## Reconciliation order

Management cluster (`mgmt/aws/`):

```
cert-manager > capi-operator > capi-system > capa-system > clusters (eu-north-1)
                                     + caaph-system > flux-apps
ack-controllers > staging-pod-identity
ack-controllers > aws-global-iam
konflate (no dependencies)
```

Workload clusters (`workload/eu-north-1-staging/`, the TrueHear layering):

```
platform (StorageClass, ALB controller) > truehear-platform
(namespaces, ServiceAccounts, backend) > keycloak, vault, redis, rabbitmq
```

## Upgrading CAPA

CAPA minors: merge one at a time and let the management cluster settle before
the next. EKS cluster versions are not Renovate-managed and upgrade
independently of the provider (see
[Dependencies](./dependencies.md)).

## Known limitations

- No S3/RDS resources: the repo declares none (the krops S3/RDS controllers
  and the `workload-resources/` example CRs were removed; the
  `docs/workload-resources.md` page describes the retired krops upstream
  posture).
- No GPU node pools: they would need a GPU instance type and quota, and the
  default run is the cheapest offered ARM and x86 shapes only.
