Dev environment overlay placeholder. Layering: workload/base (environment-neutral service roots and
charts) is composed by workload/environments/<env> (per-environment values, Ingress, SOPS Secrets)
and rendered by the sync root workload/eu-north-1-<env>. The management side for dev already exists:
the cluster definition, the dev Pod Identity associations and the flux-instance-dev ConfigMap in
mgmt/aws/addons/flux-apps/flux-instance.yaml. This directory follows the dev plan and mirrors the
staging overlay: run scripts/truehear-env-secrets.sh --env dev, then add the component
kustomizations and HelmReleases here.
