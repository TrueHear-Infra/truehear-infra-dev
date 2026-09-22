#!/usr/bin/env bash
# Vendor the TrueHear service kustomize roots, Helm charts and Vault values
# from truehear-cloud-development (private) into workload/base/, pinned to
# one commit. Byte-identical copies: `diff -r` against an upstream checkout
# must be empty for these paths. Re-run with a new SHA to refresh. The
# redis/ and rabbitmq/ chart directories are consumed by Flux HelmReleases
# with GitRepository chart sources, so the chart directories must stay
# complete (no pruned templates).
# Usage: scripts/vendor-truehear-services.sh [<commit-sha>]
set -euo pipefail
SHA="${1:-ff80819c72602f5f3483846609206e2d771de8ca}"
REPO="TrueHear/truehear-cloud-development"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fetch() { # <upstream path> <destination path>
  mkdir -p "$(dirname "$ROOT/$2")"
  env -u GITHUB_TOKEN -u GH_TOKEN gh api -H 'Accept: application/vnd.github.raw' \
    "repos/$REPO/contents/$1?ref=$SHA" > "$ROOT/$2"
  echo "fetched $2"
}
for svc in backend rabbitmq redis vault; do
  for f in kustomization.yaml namespace.yaml service-account.yaml; do
    fetch "kubernetes/services/$svc/base/$f" "workload/base/truehear-platform/services/$svc/base/$f"
  done
done
for f in base/kustomization.yaml base/namespace.yaml base/service-account.yaml \
         postgresql/kustomization.yaml postgresql/configuration.yaml postgresql/internal-service.yaml \
         postgresql/service-account.yaml postgresql/service.yaml postgresql/stateful-set.yaml \
         application/kustomization.yaml application/deployment.yaml application/disruption-budget.yaml \
         application/service.yaml; do
  fetch "kubernetes/services/keycloak/$f" "workload/base/keycloak/services/keycloak/$f"
done
for f in Chart.yaml values.yaml; do
  fetch "kubernetes/services/redis/chart/$f" "workload/base/redis/chart/$f"
done
for f in _helpers.tpl configmap.yaml headless-service.yaml statefulset.yaml; do
  fetch "kubernetes/services/redis/chart/templates/$f" "workload/base/redis/chart/templates/$f"
done
for f in Chart.yaml values.yaml; do
  fetch "kubernetes/services/rabbitmq/chart/$f" "workload/base/rabbitmq/chart/$f"
done
for f in _helpers.tpl client-service.yaml configmap.yaml headless-service.yaml pod-disruption-budget.yaml statefulset.yaml; do
  fetch "kubernetes/services/rabbitmq/chart/templates/$f" "workload/base/rabbitmq/chart/templates/$f"
done
fetch "kubernetes/services/vault/values/dev.yaml" "workload/base/vault/values.yaml"
# The staging overlay reads the same values through a configMapGenerator,
# which cannot reference files outside its own kustomization root. A future
# prod overlay adds a third destination.
fetch "kubernetes/services/vault/values/dev.yaml" "workload/environments/staging/vault/values.yaml"
echo "pinned to $REPO@$SHA"
