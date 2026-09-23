# AWS profile bootstrap runbook (truehear-infra-dev)

Source: the dev-environment plan (`.hermes/plans/2026-09-22_080221-truehear-dev-environment-aws.md`) plus `docs/truehear-environments.md`, `docs/aws.md`, `docs/aws-iam.md`, `docs/secrets.md`, `docs/operations.md` in [TrueHear-Infra/truehear-infra-dev](https://github.com/TrueHear-Infra/truehear-infra-dev), as of `feat/truehear-staging` HEAD `cd98db7` (2026-09-23).

What it does in one line: a throwaway local kind cluster runs Flux, Flux provisions the `eu-north-1-management` EKS cluster and the `eu-north-1-dev` and `eu-north-1-staging` workload clusters (3 x t3.medium across 3 AZs each) via CAPA, everything pivots into the self-managed management cluster, and each workload cluster's own Flux instance (synced from Git, decrypted with SOPS) delivers its environment: staging runs the full TrueHear stack (platform layer, Keycloak + PostgreSQL, Vault, Redis, RabbitMQ, backend), dev is provisioned but its overlay is still a placeholder.

The topology this runbook targets (staging branch, the #4 commit series):

- One region (`eu-north-1`), three EKS clusters, all EKS 1.34.6, 20 GiB gp3 roots, on-demand:
  - `eu-north-1-management` (the self-managed management cluster): 2 x t4g.medium (ARM), min 2 / max 3, AZs eu-north-1a/b/c.
  - `eu-north-1-dev` (EKS name `default_eu-north-1-dev-control-plane`): 3 x t3.medium, min 3 / max 4.
  - `eu-north-1-staging` (EKS name `default_eu-north-1-staging-control-plane`): 3 x t3.medium, min 3 / max 4.
- Each workload cluster has its own VPC (CAPA creates one per AWSManagedCluster) and its own Flux instance. The 3-node worker floor is a hard requirement, not a cost choice: Vault needs one member per zone and the RabbitMQ chart one member per node, so scaling below three leaves those StatefulSets Pending.
- Management cluster runs the ACK `iam` + `eks` controllers only. The krops S3/RDS controllers and the `Bucket`/`DBInstance`/reader-`Role` example CRs are removed; there are no S3 buckets or RDS instances in the account.
- Per-environment Pod Identity: the ACK controllers on the management cluster create, for each of dev and staging, the roles `truehear-<env>-ebs-csi` (managed policy `AmazonEBSCSIDriverPolicyV2`) and `truehear-<env>-aws-load-balancer-controller` (customer-managed policy pinned to ALB controller v3.5.0) plus two `PodIdentityAssociation`s each. The workload clusters hold no AWS credentials.
- Workload layering: `workload/platform` (ALB controller 3.5.0 from the `eks-charts` HelmRepository + `truehear-encrypted-gp3` StorageClass) -> `workload/base` (vendored service roots and the in-repo redis/rabbitmq charts) -> `workload/environments/<env>` (the overlay) -> `workload/eu-north-1-<env>` (the sync root). The staging sync root declares six Flux Kustomizations in order: `platform`, `truehear-platform`, `keycloak`, `vault`, `redis`, `rabbitmq` (the last four depend on `truehear-platform`).
- Per-environment Flux wiring in `mgmt/aws/addons/flux-apps/flux-instance.yaml`: one ConfigMap per environment (`flux-instance-dev`, `flux-instance-staging`), each carrying the workload cluster's `flux-system` namespace, its `cluster-vars` ConfigMap and the `FluxInstance` CR; and one `ClusterResourceSet` per environment (selector `fluxcd: enabled` + `environment: <env>`, strategy `ApplyOnce`) that ships the ConfigMap, the SOPS-encrypted GitHub PAT secret `flux-pull-secret`, and the `sops-age-resource-set` payload.
- Staging is the first environment to run the full stack (hostname `auth.staging.truehearkiosk.com`). The dev cluster is provisioned and receives its FluxInstance, but `workload/eu-north-1-dev` does not exist yet (`workload/environments/dev/` is a placeholder README), so dev's Flux stays in error until the dev overlay is built. That is expected on this branch.

What runs in staging (mirror-prod rule: same service set as prod, lesser hardware; `docs/truehear-environments.md` is the sizing source of truth):

| Component | Replicas | Requests (each) | Notes |
|---|---|---|---|
| Keycloak | 2 | 500m / 1Gi | PDB minAvailable 1; ALB Ingress on `auth.staging.truehearkiosk.com` |
| keycloak-postgresql | 1 | 250m / 512Mi | PVC 20Gi `truehear-encrypted-gp3` |
| Vault (chart 0.34.1, image `hashicorp/vault:2.0.4`) | 3 | 100m / 256Mi | HA raft, required anti-affinity per zone; sealed until an operator unseals it |
| Redis (in-repo chart 0.3.0, `redis:8.2.9-bookworm`) | 6 | 100m / 256Mi | PVC 10Gi; `redis-cluster-create` Job must complete after rollout |
| RabbitMQ (in-repo chart 0.1.0, `rabbitmq:4.2.9-management`) | 3 | 250m / 512Mi | required anti-affinity per node; PVC 10Gi |
| ALB controller (chart 3.5.0) | 2 | chart defaults | EKS Pod Identity, explicit `vpcId` |

Sum of requests about 5 GiB / 3.1 vCPU, which fits 3 x t3.medium with headroom for kube-system.

Before the first live run: tear down whatever the account is currently running from the fork's `main` (old krops topology: eu-west-1 workload cluster, the old eu-north-1 `staging` cluster, the S3 buckets and RDS instance). The staging branch renames that old `eu-north-1/staging` cluster definition to `dev` and adds a new `staging` one, so bootstrapping against a live old environment makes Flux delete the old `Cluster` objects and ACK CRs mid-run.

## One-time setup

### 1. Get the repo and tools

```sh
git clone https://github.com/TrueHear-Infra/truehear-infra-dev.git
cd truehear-infra-dev
git checkout feat/truehear-staging
mise install                 # host tools for validate/docs; lifecycle + helpers run in the toolbox
export TOOLBOX_IMAGE=ghcr.io/polarsquad/krops-toolbox:latest   # or krops-toolbox:dev for local builds
# helper run shape used below (same as docs/secrets.md):
krops_mise() {
  docker run --rm -it --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$PWD:/workspace" -w /workspace \
    -e MISE_AUTO_INSTALL=0 -e SOPS_AGE_KEY_FILE=/workspace/age.agekey \
    --entrypoint mise "$TOOLBOX_IMAGE" "$@"
}
```

Needs: a running Docker engine (or Podman 5.5+) and mise.

### 2. Fill in `.env`

```sh
cp .env.example .env
$EDITOR .env
```

```sh
GIT_REPO_URL="https://github.com/TrueHear-Infra/truehear-infra-dev"
GITHUB_TOKEN="<PAT with read-only Contents on that repo>"
GITHUB_USER="git"
AGE_KEY_FILE="age.agekey"
AWS_REGION="eu-north-1"
```

The PAT is what Flux uses to clone the repo inside both clusters (management cluster: the imperatively created `flux-github-pat`; workload clusters: the SOPS-encrypted `flux-pull-secret` shipped by the ClusterResourceSets). The wrapper and the toolbox mise tasks load `.env` automatically; their values win over the process environment.

### 3. Age key for SOPS

```sh
krops_mise run sops-keygen
```

Creates `./age.agekey` (gitignored) and prints the public key. The committed `*.sops.yaml` files (under `mgmt/aws/` and `workload/`) must decrypt with it:

```sh
krops_mise run sops-decrypt mgmt/aws/capi-providers/capa-system/aws-credentials.sops.yaml > /dev/null && echo OK
```

If that fails (a fork with a fresh key): put the new public key in `.sops.yaml`, then `krops_mise run sops-updatekeys` (it re-encrypts everything under `mgmt/` and `workload/`) and commit the re-encrypted files. Note the age preflight in the bootstrap now runs for every environment and auto-generates a missing `./age.agekey`; do not rely on that here, since a freshly generated key cannot decrypt the committed secrets.

### 4. AWS credentials for the host and clusterawsadm

In-cluster controllers read credentials from the SOPS-encrypted files in Git (already committed); your shell needs AWS credentials only for `clusterawsadm`, the quota requests, and the post-bootstrap identifier reads:

```sh
export AWS_REGION=eu-north-1
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
```

The principal needs: EKS + VPC + IAM role creation (CAPA), plus the static ACK principal's union scope in `docs/aws-iam.md`, which now covers both workload clusters: IAM role and customer-managed policy management scoped to `truehear-*` names, the `krops-reader` user actions, `eks:*PodIdentityAssociation*` on `default_eu-north-1-dev-control-plane` and `default_eu-north-1-staging-control-plane`, and `iam:PassRole` to `pods.eks.amazonaws.com` on `truehear-*` roles. The ACK principal is the SOPS-encrypted `aws-credentials` user; grant it outside the repo (same pattern as the CAPA grant).

### 5. CAPA IAM CloudFormation stack (first run only)

```sh
krops_mise -E aws run aws-bootstrap
# same as: clusterawsadm bootstrap iam create-cloudformation-stack --region eu-north-1
```

### 6. Service quotas (clean accounts stall here)

The run provisions three EKS clusters in `eu-north-1` (management, dev, staging), each with its own CAPA-created VPC and two NAT gateways: 6 EIPs and 3 VPCs in total. The default regional EIP quota (5) is already below that, so request increases before the first clean-account run:

```sh
aws service-quotas request-service-quota-increase --service-code ec2 --quota-code L-0263D0A3 --desired-value 8 --region eu-north-1
aws service-quotas request-service-quota-increase --service-code vpc --quota-code L-F678F1CE --desired-value 8 --region eu-north-1
```

(Both typically auto-approve in minutes. Raise the VPC value further if the account already carries non-krops VPCs.)

### 7. Optional: account-level EBS encryption

`AWSManagedMachinePool` v2.13.0 has no `encrypted` flag outside a launch template, so root-volume encryption for the workers comes from account defaults:

```sh
aws ec2 enable-ebs-encryption-by-default --region eu-north-1
```

## Run it

### 8. Bootstrap

```sh
scripts/toolbox-run.sh bootstrap aws
```

1. Creates the local `mgmt` kind cluster.
2. Installs the Flux Operator.
3. Creates the `flux-github-pat` and `sops-age` secrets, plus the `default/sops-age-resource-set` wrapper Secret (type `addons.cluster.x-k8s.io/resource-set`) that the per-environment ClusterResourceSets apply to the workload clusters so their Flux can decrypt `workload/**/*.sops.yaml`.
4. Installs a FluxInstance that syncs `mgmt/aws/` from the `git-branch` in `bootstrap.toml` (committed value: `main`).
5. Waits for CAPA to provision `eu-north-1-management` (15-25 min for EKS is normal; the wait timeout is 40 min), then pivots: moves the CAPI inventory into that EKS cluster and deletes kind.

Reruns are safe: fix whatever failed and run the same command again. `BOOTSTRAP_PIVOT=0` skips the pivot if you want to poke at the kind cluster first.

> On this host the checked-in `kind-cluster = "mgmt"` collides with the biggs.dog local-talos kind cluster. Use the scratch config instead: regenerate `bootstrap.truehear-e2e.toml` from the current `bootstrap.toml` (the CLI is `deny_unknown_fields`; it now carries the `sops-age-resource-set` and `mgmt-namespace` keys, so a stale copy is rejected) and run the raw form `docker run ... -e BOOTSTRAP_CONFIG=bootstrap.truehear-e2e.toml "$TOOLBOX_IMAGE" aws` (the wrapper does not forward `BOOTSTRAP_CONFIG`).

> Running the branch before it is merged: the committed sync targets still say `main` (the `git-branch` in `bootstrap.toml` and the `ref: "refs/heads/main"` inside both `flux-instance-<env>` ConfigMaps). To exercise the staging branch end to end, set `git-branch = "feat/truehear-staging"` in the checkout's `bootstrap.toml` (or the scratch config) and patch both ConfigMap `ref`s to `refs/heads/feat/truehear-staging` before the run.

### 9. Commit the post-bootstrap identifiers

Each environment's `cluster-vars` ConfigMap (in `mgmt/aws/addons/flux-apps/flux-instance.yaml`) renders the ALB controller with `${VPC_ID}` and the Keycloak Ingress with `${KEYCLOAK_ACM_CERTIFICATE_ARN}`. Both are unknown until the cluster exists, and the ConfigMap reaches the workload cluster via an `ApplyOnce` ClusterResourceSet, so this step has two parts. Staging is the environment that matters on this branch; the dev pair can be filled at the same time (the dev VPC is `eu-north-1-dev-vpc`) or when the dev overlay lands:

```sh
# 1. Read the staging VPC ID (CAPA names the VPC <cluster-name>-vpc)
aws ec2 describe-vpcs --filters Name=tag:Name,Values=eu-north-1-staging-vpc \
  --query 'Vpcs[0].VpcId' --output text --region eu-north-1
```

Edit the `flux-instance-staging` data in `mgmt/aws/addons/flux-apps/flux-instance.yaml`: replace `VPC_ID: "REPLACE_AFTER_FIRST_BOOTSTRAP"` with the real VPC ID and `KEYCLOAK_ACM_CERTIFICATE_ARN: "REPLACE_WITH_ACM_CERT_ARN"` with the ACM certificate ARN for `auth.staging.truehearkiosk.com`. Then commit and push.

2. Re-apply the `cluster-vars` ConfigMap to the staging cluster by hand (ApplyOnce never re-fires), using the staging kubeconfig from step 10:

```sh
kubectl --kubeconfig .kube/krops-workloads.yaml -n flux-system apply -f - <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: cluster-vars
  namespace: flux-system
data:
  AWS_REGION: "eu-north-1"
  CLUSTER_NAME: "eu-north-1-staging"
  EKS_CLUSTER_NAME: "default_eu-north-1-staging-control-plane"
  AWS_ACCOUNT_ID: "974771261200"
  ENVIRONMENT: "staging"
  VPC_ID: "vpc-<from-step-1>"
  KEYCLOAK_HOSTNAME: "auth.staging.truehearkiosk.com"
  KEYCLOAK_ACM_CERTIFICATE_ARN: "arn:aws:acm:eu-north-1:974771261200:certificate/<uuid>"
EOF
export KUBECONFIG="$PWD/.kube/krops-workloads.yaml"
kubectl config use-context eu-north-1-staging
flux reconcile kustomization platform --with-source
```

Until `VPC_ID` is real, the ALB controller HelmRelease stays not-Ready: pods on EKS managed node groups have IMDS hop limit 1 and cannot discover the VPC through the metadata service.

### 10. Verify the management cluster

```sh
export KUBECONFIG="$PWD/.kube/krops-mgmt.yaml"    # written by the toolbox run
flux get kustomizations --watch                   # all Ready, incl. ack-controllers, dev-pod-identity, staging-pod-identity
kubectl get clusters.cluster.x-k8s.io -A          # eu-north-1-management, eu-north-1-dev, eu-north-1-staging Provisioned
kubectl get roles.iam.services.k8s.aws -n ack-system                 # truehear-{dev,staging}-{ebs-csi,aws-load-balancer-controller}, krops-reader
kubectl get policies.iam.services.k8s.aws -n ack-system              # truehear-{dev,staging}-aws-load-balancer-controller
kubectl -n ack-system get podidentityassociations.eks.services.k8s.aws \
  -o custom-columns='NAME:.metadata.name,SYNCED:.status.conditions[?(@.type=="ACK.ResourceSynced")].status'
```

All four `PodIdentityAssociation`s (two per environment) must be `True` once their control plane is ACTIVE; until then the `Ready` condition says `ResourceNotFoundException`, no action needed. There are deliberately no `buckets`/`dbinstances` resources anymore; `kubectl get buckets.s3.services.k8s.aws` errors with no CRD.

### 11. Verify the staging cluster

The `kubeconfigs` mise task assumes the krops upstream cluster naming (`default_<region>-workload-control-plane`), so export the TrueHear kubeconfigs directly:

```sh
aws eks update-kubeconfig --name default_eu-north-1-staging-control-plane \
  --region eu-north-1 --kubeconfig .kube/krops-workloads.yaml --alias eu-north-1-staging
export KUBECONFIG="$PWD/.kube/krops-workloads.yaml"
kubectl config use-context eu-north-1-staging
flux get kustomizations -n flux-system          # platform, truehear-platform, keycloak, vault, redis, rabbitmq all True
kubectl -n kube-system get pods -l app.kubernetes.io/name=aws-load-balancer-controller   # 2/2 Running
kubectl get sc truehear-encrypted-gp3
kubectl -n keycloak get pods                    # keycloak 2/2, keycloak-postgresql 1/1
kubectl -n keycloak get pvc                     # Bound (on truehear-encrypted-gp3)
kubectl -n vault get pods                       # 3/3 (sealed until init, see Day-2)
kubectl -n redis get pods                       # 6/6
kubectl -n redis get jobs                       # redis-cluster-create Complete
kubectl -n rabbitmq get pods                    # 3/3
kubectl -n keycloak get ingress keycloak -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'
```

From here, every change is a commit to the repo; Flux reconciles it. Do not `kubectl apply` or edit live objects (except reading).

## Day-2 workflow

### Generating the environment secrets

`mise run truehear-env-secrets -- --env <env>` (replaces the old dev-only `keycloak-dev-secrets` task) generates every SOPS-encrypted Secret the overlay for `<env>` needs, all eleven of them: `keycloak-bootstrap-admin`, `keycloak-postgresql-credentials`, `keycloak-postgresql-tls`, `keycloak-tls`, `vault-tls`, `redis-tls`, `redis-auth`, `redis-backend-acl`, `rabbitmq-tls`, `rabbitmq-erlang-cookie`, `rabbitmq-bootstrap-auth`. One throwaway CA per run signs all leaves, so `ca.crt` is the same in every TLS Secret; nothing plaintext touches the working tree. `--keycloak-tls DIR` / `--postgresql-tls DIR` / `--vault-tls DIR` / `--redis-tls DIR` / `--rabbitmq-tls DIR` swap in PKIv2-issued leaves (each dir: `tls.crt`, `tls.key`, `ca.crt`) instead of the generated ones. Refuses to overwrite existing files without `--force` (that is the rotation path). Commit the re-encrypted files; the workload Flux re-decrypts with the `sops-age` Secret the CRS already delivered.

### Vault: init and unseal

The `vault` Kustomization is Ready when the StatefulSet has rolled out, but the Pods stay sealed until an operator initialises and unseals Vault. Follow the upstream Vault infrastructure setup manual in TrueHear/truehear-cloud-development `documentation/services/vault` (init, unseal, token handling, the Kubernetes TLS Secret). KMS auto-unseal is a follow-up.

### Redis: the cluster-create Job

The Redis HelmRelease is Ready before the cluster is formed. Check that the `redis-cluster-create` Job in the `redis` namespace completed; the Job's completion is the second signal.

### RabbitMQ: rotate the bootstrap user

The chart creates its initial admin from the `rabbitmq-bootstrap-auth` Secret. After first access, set a new admin password imperatively and commit the re-encrypted Secret (via `truehear-env-secrets --force`) so Git is the source of truth again.

### Route 53

The Keycloak ALB Ingress is internet-facing. Create a Route 53 alias record (A/AAAA) for `auth.staging.truehearkiosk.com` pointing at the ALB whose name the Ingress annotation pins (`truehear-staging-keycloak`); the ALB's DNS name is in the Ingress status. external-dns is a follow-up issue.

### Age-key or PAT rotation

The ClusterResourceSets are `ApplyOnce`: a rotated key or PAT lands in the management cluster's `sops-age-resource-set` / `flux-pull-secret`, but each workload cluster keeps the old copy until you re-apply the payload (on staging now; on dev too once it is reconciling):

```sh
kubectl --kubeconfig .kube/krops-workloads.yaml -n flux-system apply -f - \
  <(kubectl --kubeconfig .kube/krops-mgmt.yaml -n default get secret sops-age-resource-set -o jsonpath='{.data.sops-age\.yaml}' | base64 -d)
```

A Kustomization that fails decryption surfaces `no keys found` in its conditions.

### Known traps

- CAPA's `spec.addons` on each workload control plane is the COMPLETE desired set: dropping `eks-pod-identity-agent` or `aws-ebs-csi-driver` from `mgmt/aws/clusters/eu-north-1/<env>/cluster.yaml` silently uninstalls the add-on and the Pod Identity associations stop working (PVCs stay Pending, ALB Ingress never binds).
- The ALB controller ServiceAccount name (`aws-load-balancer-controller` in `kube-system`) is a contract with `mgmt/aws/infrastructure/<env>-pod-identity/associations.yaml` per environment; changing one without the other orphans the association.
- The ALB controller chart comes from the `eks-charts` HelmRepository (`https://aws.github.io/eks-charts`); the ECR Public OCI path does not exist. Bump the IAM `Policy` CR together with the chart version (the policy document is pinned to v3.5.0).
- Vault and Redis "Ready" is not the end state: unseal Vault and wait for the `redis-cluster-create` Job before treating the platform as usable.
- The 3-node worker floor is a hard requirement (Vault per zone, RabbitMQ per node), not a cost choice.
- Route 53 is manual today (see above); the EKS API endpoint and the ALB are open, CIDR restriction is a follow-up.
- Vendored service roots under `workload/base/` are byte-identical copies from `TrueHear/truehear-cloud-development@ff80819c` (the default SHA in `scripts/vendor-truehear-services.sh`); refresh with `scripts/vendor-truehear-services.sh <sha>` and `diff -r` against the upstream paths to prove byte identity.
- The `kubeconfigs` mise task in `mise.aws.toml` is the krops upstream version and does not match the TrueHear cluster names; export with `aws eks update-kubeconfig` per cluster (step 11).

## When done

### Teardown

```sh
scripts/toolbox-run.sh teardown aws
```

Deletes the workload Cluster objects (dev and staging), then sweeps AWS per environment-level target from `bootstrap.toml` (`eu-north-1-dev` and `eu-north-1-staging`), the self-managed management cluster (through the orphan sweep, never its own Cluster object), and the kind cluster. The sweep covers nodegroups, EKS control planes, CAPA-tagged VPCs and security groups, the CAPA per-cluster IAM roles (name prefix `eu-north-1-<env>`), the four `truehear-<env>-*` Pod Identity roles (with their attached/inline policies), the `krops-reader` user, and the CAPA CloudFormation stack. The two customer-managed ALB `Policy` resources (the `truehear-<env>-aws-load-balancer-controller` policies) are NOT deleted by the sweep: delete them manually afterward (`aws iam delete-policy`) or they orphan in the account. There is no S3/RDS sweep (the repo declares none; the `rds-instance` sweep fields name identifiers that never exist, so the sweep reports "not found"). If the management cluster is already unreachable, `AWS_ONLY=1` (raw container or native run; the wrapper does not forward it) runs just the AWS sweep.
