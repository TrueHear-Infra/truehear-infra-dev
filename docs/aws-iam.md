# AWS authentication & IAM

## ACK controllers on the management cluster (static SOPS credentials)

All ACK controllers (IAM, EKS) run on the **management** cluster only
(issue #346). ACK controllers talk to the AWS API directly, so they do not
need to run inside the cluster whose resources they manage; the workload
clusters run no controllers and hold no credentials at all.

The purpose here is Pod Identity plumbing for each TrueHear workload cluster
(dev, staging): the management cluster's ACK IAM controller creates the two
controller roles (and the ALB controller's customer-managed policy) for
every workload cluster, and the ACK EKS controller binds them to their
ServiceAccounts on each cluster. The workload clusters therefore assume
their IAM roles through EKS Pod Identity and hold no AWS credentials of
their own.

The controllers authenticate with the same SOPS-encrypted static credential
pattern as CAPA
(`mgmt/aws/infrastructure/ack-controllers/aws-credentials.sops.yaml`). The
bootstrap cluster runs on kind (not EKS), so IRSA/Pod Identity is not
available before the pivot; static credentials via SOPS is the established
pattern.

### Least-privilege trade-off: the static principal's union scope

Before the pivot each workload-cluster controller assumed its own scoped
IAM role via EKS Pod Identity. Moving the controllers to the management
cluster meant the single static principal behind `aws-credentials` needs
the **union** of its former policies (granted outside this repo, same as
the CAPA permissions):

- **IAM**: role management (`iam:CreateRole`/`DeleteRole`/`GetRole`/
  `UpdateRole`/`UpdateRoleDescription`/`UpdateAssumeRolePolicy`/
  `PutRolePolicy`/`DeleteRolePolicy`/`GetRolePolicy`/`ListRolePolicies`/
  `AttachRolePolicy`/`DetachRolePolicy`/`ListAttachedRolePolicies`/
  `ListInstanceProfilesForRole`/`TagRole`/`UntagRole`/`ListRoleTags`) scoped
  to `arn:aws:iam::*:role/truehear-*`; customer-managed policy management
  (`iam:CreatePolicy`/`DeletePolicy`/`GetPolicy`/`GetPolicyVersion`/
  `CreatePolicyVersion`/`DeletePolicyVersion`/`ListPolicyVersions`/
  `TagPolicy`/`UntagPolicy`) scoped to `arn:aws:iam::*:policy/truehear-*`;
  plus the user actions for the `krops-reader` console user
  (`iam:CreateUser`/`PutUserPolicy`/`GetUser`/`GetUserPolicy`/`TagUser`)
- **EKS**: Pod Identity association management
  (`eks:CreatePodIdentityAssociation`/`DescribePodIdentityAssociation`/
  `UpdatePodIdentityAssociation`/`DeletePodIdentityAssociation`/
  `ListPodIdentityAssociations`/`TagResource`/`UntagResource`) on
  `arn:aws:eks:eu-north-1:*:cluster/default_eu-north-1-dev-control-plane` and
  `arn:aws:eks:eu-north-1:*:podidentityassociation/default_eu-north-1-dev-control-plane/*`,
  and `iam:PassRole` on `arn:aws:iam::*:role/truehear-*` with
  `iam:PassedToService: pods.eks.amazonaws.com`

The trade-off is real: name-scoped `iam:CreateRole` + `iam:PutRolePolicy` is
still a privilege-escalation surface (any permission can be granted to a
role, as long as it is named `truehear-*`), and the union now sits on one
long-lived static principal instead of short-lived pod-identity
sessions. Accepted because the management cluster is already the only
cluster with static AWS credentials in Git and already owns every `Cluster`
object; the workload clusters shed their last credential and controller in
exchange.

## Pod Identity roles for the workload clusters (dev, staging)

For each workload cluster, `mgmt/aws/infrastructure/<env>-pod-identity/`
(`dev-pod-identity/`, `staging-pod-identity/`) has the management cluster's
ACK IAM controller create the two roles that cluster's platform
controllers assume, and its ACK EKS controller create the associations
that bind each role to one ServiceAccount:

| Association CR | Role | Namespace | ServiceAccount |
|---|---|---|---|
| `truehear-<env>-ebs-csi` | `truehear-<env>-ebs-csi` | `kube-system` | `ebs-csi-controller-sa` |
| `truehear-<env>-aws-load-balancer-controller` | `truehear-<env>-aws-load-balancer-controller` | `kube-system` | `aws-load-balancer-controller` |

- kind: `eks.services.k8s.aws/v1alpha1 PodIdentityAssociation`, declared in
  the `ack-system` namespace of the management cluster
  (`<env>-pod-identity/associations.yaml`), `clusterName`
  `default_eu-north-1-<env>-control-plane`, the EKS name CAPA gives the
  control plane
- trust policy: the `pods.eks.amazonaws.com` service with
  `sts:AssumeRole` + `sts:TagSession` (Pod Identity's session-tagging
  condition), declared on both roles (`<env>-pod-identity/roles.yaml`)
- policies:
  - `truehear-<env>-ebs-csi` attaches the AWS **managed** policy
    `arn:aws:iam::aws:policy/AmazonEBSCSIDriverPolicyV2`, the V2 name was
    verified against the account, so it is preferred over the V1
  - `truehear-<env>-aws-load-balancer-controller` gets a **customer-managed**
    `iam.services.k8s.aws/v1alpha1 Policy` CR
    (`<env>-pod-identity/policies.yaml`) carrying the AWS Load Balancer
    Controller's policy pinned to controller v3.5.0, the chart version the
    workload layer installs (bump the policy document together with the
    chart)

Operator note: the associations' `Ready` condition message says
`ResourceNotFoundException` until the cluster's control plane exists (and is
ACTIVE with the `eks-pod-identity-agent` add-on); no action needed, Flux
reconciles this Kustomization with `wait: false`, so the delay is not an
error.

## Console access: the `krops-reader` IAM user

`mgmt/aws/infrastructure/aws-global-iam/reader-user.yaml` has the
**management** cluster's ACK IAM controller create one IAM `User`
(`krops-reader`) whose only permission is `sts:AssumeRole` on
`arn:aws:iam::*:role/truehear-*`: it can see nothing directly and is
just a doorway into the Pod Identity roles above.

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
2. Use **Switch Role** (account menu, top right) with the account ID and
   the role's name (for example `truehear-dev-ebs-csi`, or the
   `truehear-staging-*` equivalent), or use the direct link:

   ```
   https://signin.aws.amazon.com/switchrole?roleName=truehear-<env>-ebs-csi&account=<account-id>
   ```

3. Browse the repo-created resources (switch the console region to
   eu-north-1).

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
- Five customer-managed policies, grouped by concern:
  `krops-ci-e2e-capa-ec2`, `krops-ci-e2e-capa-eks`, `krops-ci-e2e-ack-mgmt`,
  `krops-ci-e2e-sweep`, `krops-ci-e2e-self-deny`.

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
  `eu-north-1` (`aws:RequestedRegion`); mutations (delete, modify,
  attach/detach, associate, authorize/revoke, release) only on resources
  carrying a `sigs.k8s.io/cluster-api-provider-aws/role` tag (CAPA-created),
  plus security-group rule and delete operations on the EKS-created cluster
  security groups (`aws:eks:cluster-name` = `default_*`);
  `CreateTags`/`DeleteTags` only with CAPA tag keys.
- EKS (`krops-ci-e2e-capa-eks`): cluster, nodegroup, addon, and access-entry
  CRUD scoped to `default_*` clusters (the non-krops `training` cluster is
  out of scope); the two EKS service-linked roles; CAPA per-cluster IAM
  roles on `capa_*` and the cluster name prefixes; `iam:PassRole` to
  `eks.amazonaws.com` for the clusterawsadm stack roles and `capa_*`; SSM
  optimized-AMI parameters, KMS grants on
  `alias/cluster-api-provider-aws-*`, Secrets Manager on
  `aws.cluster.x-k8s.io/*` secrets.
- Management ACK controllers (`krops-ci-e2e-ack-mgmt`): `truehear-*` IAM
  role and policy management (the `truehear-dev-*` roles, the
  `krops-reader` user) and pod identity associations on `default_*`
  clusters. No OIDC provider management anywhere: IRSA is unused (pod
  identity instead), which also protects the GitHub OIDC provider itself.
- Orphan sweep (`krops-ci-e2e-sweep`): `sts:GetCallerIdentity`; IAM cleanup
  on `truehear-*`/`krops-*`/`capa_*` roles, their instance profiles, and
  `krops-*` users; CloudFormation delete on the
  `cluster-api-provider-aws-sigs-k8s-io` stack.
- Self-escalation guard (`krops-ci-e2e-self-deny`): explicit Deny on the
  role's own ARN for `PutRolePolicy`, `DeleteRolePolicy`,
  `AttachRolePolicy`, `DetachRolePolicy`, `UpdateAssumeRolePolicy`,
  `DeleteRole`, and permissions-boundary changes. The `role/truehear-*`
  glob in `ack-mgmt` and the `role/krops-*` globs in `sweep` match
  `krops-ci-e2e` itself; without this Deny a workflow session could grant
  itself arbitrary inline permissions or widen its own trust (verified
  with `simulate-principal-policy`: the four escalation actions are now
  `explicitDeny`, lifecycle actions on other `truehear-*` roles remain
  `allowed`).

Narrowed relative to the upstream CAPA policy, with the repo as ground
truth: no `ec2:RunInstances`/`TerminateInstances` (no EC2 machine pools), no
ELB/Auto Scaling writes (no `AWSMachinePool` or `AWSCluster`), no spot or
fargate service-linked roles (all pools `onDemand`, no fargate profiles), no
IPAM/IPv6 actions (IPv4 clusters), no launch-template writes (the
`AWSManagedMachinePool` specs declare none).

### Not covered (operator steps)

- First-time creation of the clusterawsadm CloudFormation stack
  (the `aws-bootstrap` task, run in the toolbox per [docs/aws.md](./aws.md)) needs `cloudformation:CreateStack` plus
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
