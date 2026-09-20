# AWS authentication & IAM

## EKS Pod Identity (no static keys on workload clusters)

The ACK S3, RDS, and IAM controllers on the workload clusters carry
**no credentials**. Instead:

- Each EKS control plane enables the `eks-pod-identity-agent` addon
  (declared in the CAPI cluster spec).
- The **management** cluster runs ACK IAM + EKS controllers
  (`mgmt/aws/infrastructure/ack-controllers/`, authenticated with the same
  SOPS-encrypted credential pattern as CAPA) which declaratively create:
  - an IAM `Role` per controller, trusted by `pods.eks.amazonaws.com`:
    - `krops-ack-s3-controller`: scoped to `krops-*` buckets only
    - `krops-ack-rds-controller`: RDS management scoped to `krops-*`
      RDS resources (plus read-only `rds:Describe*`), the
      `secretsmanager:CreateSecret`/`TagResource`/`RotateSecret` actions on
      `rds!*` secrets required by `manageMasterUserPassword`,
      `kms:DescribeKey` and grant management (`CreateGrant`/`ListGrants`/
      `RevokeGrant`, restricted with `kms:GrantIsForAWSResource`) so RDS can
      use the default `aws/rds` and `aws/secretsmanager` KMS keys (without
      these `CreateDBInstance` fails with `KMSKeyNotAccessibleFault`) and
      `iam:CreateServiceLinkedRole` for `AWSServiceRoleForRDS` (needed the
      first time an RDS instance is created in the account)
    - `krops-ack-iam-controller`: IAM role management scoped to
      `krops-*` roles only. Known trade-off: name-scoped `iam:CreateRole`
      + `iam:PutRolePolicy` is still a privilege-escalation surface (any
      permission can be granted to a role, as long as it is named
      `krops-*`), consistent with the pragmatic name-based scoping used
      for the other controllers
  - a `PodIdentityAssociation` per cluster and controller binding the
    `ack-s3-controller` / `ack-rds-controller` / `ack-iam-controller`
    ServiceAccounts to their roles.

Pod Identity is used instead of IRSA because its trust policy is static: it
does not embed a per-cluster OIDC provider ID, so the whole chain can live in
Git before the clusters exist. ACK retries the associations until CAPA has
finished provisioning the EKS clusters.

## Per-cluster read-only IAM roles

`workload/base/iam-roles/role.yaml` has each cluster's ACK IAM controller create
one read-only IAM role (`krops-<cluster>-reader`). IAM is global, so the
cluster name is part of the role name to keep the two clusters from fighting
over one role:

- trust policy: the AWS account root (`arn:aws:iam::<account>:root`,
  `sts:AssumeRole`): any principal in the account that is itself allowed to
  assume the role can use it
- read-only permissions covering the resources this repo creates on **both**
  clusters: `krops-*` S3 buckets (bucket + object reads) and `krops-*`
  RDS instances (`rds:DescribeDBInstances`, `rds:ListTagsForResource`:
  only Describe actions that support resource-level scoping)

## Console access: the `krops-reader` IAM user

`mgmt/aws/infrastructure/aws-global-iam/reader-user.yaml` has the
**management** cluster's ACK IAM controller create one IAM `User`
(`krops-reader`) whose only permission is `sts:AssumeRole` on
`arn:aws:iam::*:role/krops-*-reader`: it can see nothing directly and is
just a doorway into the per-cluster reader roles above.

The ACK IAM controller has no `LoginProfile` resource, so the console
password cannot be declared in Git. Set it **once** imperatively after the
user has been reconciled:

```sh
aws iam create-login-profile --user-name krops-reader \
  --password '<initial-password>' --password-reset-required
```

Then, to browse the repo-created resources in the AWS console:

1. Sign in at `https://<account-id>.signin.aws.amazon.com/console` as
   `krops-reader` (you will be prompted to set a new password on first
   login).
2. Use **Switch Role** (account menu, top right) with the account ID and role
   name `krops-eu-north-1-workload-reader` or
   `krops-eu-west-1-workload-reader`, or use the direct link:

   ```
   https://signin.aws.amazon.com/switchrole?roleName=krops-eu-north-1-workload-reader&account=<account-id>
   ```

3. Browse the `krops-*` S3 buckets and RDS instances (switch the console
   region to eu-north-1/eu-west-1 for the databases).

## CI access: GitHub Actions OIDC and the `krops-ci-e2e` role

CI authenticates to the e2e account (`120392301094`) through GitHub's OIDC
provider and assumes a least-privilege role for the e2e lifecycle (issue
#379). No AWS access keys are stored; every session is short-lived. Created
imperatively, additive only.

### Resources

- OIDC provider:
  `arn:aws:iam::120392301094:oidc-provider/token.actions.githubusercontent.com`
  (audience `sts.amazonaws.com`).
- Role: `arn:aws:iam::120392301094:role/krops-ci-e2e` (max session 4 hours).
- Four customer-managed policies, grouped by concern:
  `krops-ci-e2e-capa-ec2`, `krops-ci-e2e-capa-eks`, `krops-ci-e2e-ack-mgmt`,
  `krops-ci-e2e-sweep`.

### Trust policy

The role trusts only the GitHub OIDC provider with audience
`sts.amazonaws.com` and `sub` matching `repo:polarsquad/krops:*` (any ref in
that repo). To narrow later, replace the `StringLike` `sub` value with
explicit refs, for example
`repo:polarsquad/krops:ref:refs/heads/main`, via
`iam:UpdateAssumeRolePolicy`.

### Permission groupings

Derived from the repo, not guesswork: the CAPA surface comes from the pinned
`clusterawsadm` 2.13.0 `print-policy` output evaluated with krops' feature
gates (`EKS=true,EKSEnableIAM=true,EKSAllowAddRoles=true,MachinePool=true`),
the sweep surface from `teardown.sh` / `bootstrap-rs/src/teardown.rs`, and
the ACK surface from the prerequisites in [AWS environment](./aws.md).

- EC2/VPC (`krops-ci-e2e-capa-ec2`): the full describe set; creates only in
  `eu-north-1` and `eu-west-1` (`aws:RequestedRegion`); mutations (delete,
  modify, attach/detach, associate, authorize/revoke, release) only on
  resources carrying a `sigs.k8s.io/cluster-api-provider-aws/role` tag
  (CAPA-created), plus security-group rule and delete operations on the
  EKS-created cluster security groups (`aws:eks:cluster-name` = `default_*`);
  `CreateTags`/`DeleteTags` only with CAPA tag keys.
- EKS (`krops-ci-e2e-capa-eks`): cluster, nodegroup, addon, and access-entry
  CRUD scoped to `default_*` clusters in the two e2e regions (the non-krops
  `training` cluster is out of scope); the two EKS service-linked roles;
  CAPA per-cluster IAM roles on `capa_*` and the cluster name prefixes;
  `iam:PassRole` to `eks.amazonaws.com` for the clusterawsadm stack roles
  and `capa_*`; SSM optimized-AMI parameters, KMS grants on
  `alias/cluster-api-provider-aws-*`, Secrets Manager on
  `aws.cluster.x-k8s.io/*` secrets.
- Management ACK controllers (`krops-ci-e2e-ack-mgmt`): `krops-*` IAM role
  and user management (the `krops-ack-*` roles, the `krops-*-reader` roles,
  the `krops-reader` user) and pod identity associations on `default_*`
  clusters. No OIDC provider management anywhere: IRSA is unused (pod
  identity instead), which also protects the GitHub OIDC provider itself.
- Orphan sweep (`krops-ci-e2e-sweep`): `sts:GetCallerIdentity`; RDS delete
  on `krops-*` instances; S3 version purge and bucket delete on `krops-*`
  buckets; IAM cleanup on `krops-*`/`capa_*` roles, their instance profiles,
  and `krops-*` users; CloudFormation delete on the
  `cluster-api-provider-aws-sigs-k8s-io` stack in both regions.
- Self-escalation guard (`krops-ci-e2e-self-deny`): explicit Deny on the
  role's own ARN for `PutRolePolicy`, `DeleteRolePolicy`,
  `AttachRolePolicy`, `DetachRolePolicy`, `UpdateAssumeRolePolicy`,
  `DeleteRole`, and permissions-boundary changes. The `role/krops-*` globs
  in `ack-mgmt` and `sweep` match `krops-ci-e2e` itself; without this Deny a
  workflow session could grant itself arbitrary inline permissions or widen
  its own trust (verified with `simulate-principal-policy`: the four
  escalation actions are now `explicitDeny`, lifecycle actions on other
  `krops-*` roles remain `allowed`).

Narrowed relative to the upstream CAPA policy, with the repo as ground
truth: no `ec2:RunInstances`/`TerminateInstances` (no EC2 machine pools), no
ELB/Auto Scaling writes (no `AWSMachinePool` or `AWSCluster`), no spot or
fargate service-linked roles (all pools `onDemand`, no fargate profiles), no
IPAM/IPv6 actions (IPv4 clusters), no launch-template writes (the
`AWSManagedMachinePool` specs declare none).

### Not covered (operator steps)

- First-time creation of the clusterawsadm CloudFormation stack
  (`mise -E aws run aws-bootstrap`) needs `cloudformation:CreateStack` plus
  IAM writes on the `*.cluster-api-provider-aws.sigs.k8s.io` roles. The
  stack is already `CREATE_COMPLETE` (2026-09-01, per #143), so recreating
  it stays an operator step.
- Service-quota increases (see [Operations](./operations.md)) and the
  one-time `iam:CreateLoginProfile` for `krops-reader` (above) stay operator
  steps.
- The committed `aws-credentials.sops.yaml` secrets (CAPA and the management
  ACK controllers after the pivot) still hold the `capi-demo` credential
  profile; the OIDC role covers the ambient/script surface and the
  pre-pivot bootstrap cluster. Replacing the committed secret is part of
  the #185 wiring.

### Assuming the role from a workflow

```yaml
permissions:
  id-token: write
  contents: read
steps:
  - uses: aws-actions/configure-aws-credentials@v4
    with:
      role-to-assume: arn:aws:iam::120392301094:role/krops-ci-e2e
      aws-region: eu-north-1
```

The action exchanges the workflow's OIDC token for session credentials;
nothing is stored. The role is not wired into any workflow yet; CI
integration is tracked in #185.

### Revocation

- Cut all CI access: delete the OIDC provider
  (`aws iam delete-open-id-connect-provider --open-id-connect-provider-arn
  arn:aws:iam::120392301094:oidc-provider/token.actions.githubusercontent.com`).
  The role remains but can no longer be assumed from GitHub. Note this stops
  NEW assumptions only: sessions already issued run until their granted
  expiry (4 hour maximum), so access is not cut instantly during an
  incident.
- Narrow instead: tighten the trust policy's `sub` condition to specific
  refs (above).
- Rotate: nothing to rotate; STS sessions are short-lived (1 hour typical,
  4 hour maximum) and independent of the short-lived OIDC token.
