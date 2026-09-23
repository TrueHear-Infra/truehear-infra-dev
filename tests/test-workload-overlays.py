#!/usr/bin/env python3
"""Cross-checks for the TrueHear workload layering (workload/*).

Each TrueHear workload cluster syncs its own sync root
workload/eu-north-1-<env> (mgmt/aws/addons/flux-apps/flux-instance.yaml).
This test pins the invariants across those files:

- every ${VAR} used in the AWS TrueHear tree exists in every
  environment's cluster-vars ConfigMap (the intersection of the blocks
  in flux-instance.yaml),
- no literal subnet IDs, ACM ARNs or account IDs in non-sops manifests,
- every Flux Kustomization sources GitRepository flux-system and
  substitutes cluster-vars (the two legacy operator roots that need no
  substitution are excepted by name),
- the ALB controller ServiceAccount appears in every pod-identity
  associations.yaml,
- each sync root declares the six Kustomizations in order and renders,
- every environment overlay with a kustomization.yaml has a sync root
  and a FluxInstance ConfigMap,
- the five staging Flux roots render pairwise-disjoint object sets
  (backend is a child of the truehear-platform root, not a Flux root).

Runs kubectl (mise) from PATH. Usage: mise exec -- python3 tests/test-workload-overlays.py
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FLUX_INSTANCE = REPO_ROOT / "mgmt/aws/addons/flux-apps/flux-instance.yaml"
ASSOCIATION_GLOB = "mgmt/aws/infrastructure/*-pod-identity/associations.yaml"
LBC_HELM = REPO_ROOT / "workload/platform/aws-load-balancer-controller/helm.yaml"
EXPECTED_KS = ["platform", "truehear-platform", "keycloak", "vault", "redis", "rabbitmq"]
# The five staging Flux roots. backend renders only through the
# truehear-platform root, so it is not a Flux root and is excluded.
FLUX_ROOTS = [
    "workload/environments/staging",
    "workload/environments/staging/keycloak",
    "workload/environments/staging/vault",
    "workload/environments/staging/redis",
    "workload/environments/staging/rabbitmq",
]
# Legacy GCP operator roots that deliberately carry no
# postBuild.substituteFrom: kcc-operator ships its webhook certs in the
# pinned bundle.
NO_SUBSTITUTION = {
    "workload/gcp-base/kcc-operator/flux-ks.yaml",
}
FORBIDDEN = re.compile(r"subnet-[0-9a-f]{8,}|arn:aws:acm:|974771261200|120392301094")


def aws_scan_dirs():
    """The AWS TrueHear workload tree, the only consumer of the AWS
    cluster-vars ConfigMap. The legacy gcp-base root and its sync root
    substitute from their own environment ConfigMap channel, so their
    variables are outside this check."""
    dirs = [
        REPO_ROOT / "workload/base",
        REPO_ROOT / "workload/platform",
        REPO_ROOT / "workload/environments",
    ]
    dirs += sorted((REPO_ROOT / "workload").glob("eu-north-1-*"))
    return [d for d in dirs if d.is_dir()]


def cluster_vars_keys():
    """Intersection of the keys of every cluster-vars block in
    flux-instance.yaml: a variable used under workload/ must exist in
    every environment's ConfigMap, not just one."""
    text = FLUX_INSTANCE.read_text()
    parts = re.split(r"^\s+name: cluster-vars\s*$", text, flags=re.M)[1:]
    key_sets = []
    for part in parts:
        block = part.split("---", 1)[0]
        key_sets.append(set(re.findall(r"^\s+([A-Z_]+): ", block, re.M)))
    if not key_sets:
        return set()
    keys = set(key_sets[0])
    for other in key_sets[1:]:
        keys &= other
    return keys


def rendered_objects(stdout):
    objs = set()
    for doc in stdout.split("\n---"):
        kind = re.search(r"^kind: (\S+)", doc, re.M)
        name = re.search(r"^  name: (\S+)", doc, re.M)
        namespace = re.search(r"^  namespace: (\S+)", doc, re.M)
        if kind and name:
            objs.add((kind.group(1), namespace.group(1) if namespace else "-", name.group(1)))
    return objs


def main() -> int:
    failures = []

    keys = cluster_vars_keys()
    if not keys:
        failures.append("no cluster-vars block found in flux-instance.yaml")
    used = set()
    for path in sorted((REPO_ROOT / "workload").rglob("*.yaml")):
        text = path.read_text()
        if not path.name.endswith(".sops.yaml") and FORBIDDEN.search(text):
            failures.append(f"{path.relative_to(REPO_ROOT)}: account/subnet/ACM literal; use cluster-vars")
    for d in aws_scan_dirs():
        for path in sorted(d.rglob("*.yaml")):
            used |= set(re.findall(r"\$\{([A-Z_]+)(?::=[^}]*)?\}", path.read_text()))
    for var in sorted(used - keys):
        failures.append(f"${{{var}}} used in the AWS workload tree but not in every cluster-vars ConfigMap")

    for path in sorted((REPO_ROOT / "workload").rglob("flux-ks.yaml")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        docs = [d for d in path.read_text().split("\n---") if "kind: Kustomization" in d]
        for d in docs:
            match = re.search(r"^\s+name: (\S+)", d, re.M)
            name = match.group(1) if match else None
            if "kind: GitRepository" not in d:
                failures.append(f"{rel}: Kustomization {name} must source GitRepository flux-system")
            if rel not in NO_SUBSTITUTION and "name: cluster-vars" not in d:
                failures.append(f"{rel}: Kustomization {name} lacks substituteFrom cluster-vars")

    sa = re.search(r"serviceAccount:\s*\n\s+name: (\S+)", LBC_HELM.read_text()) or re.search(
        r"name: (aws-load-balancer-controller)\n", LBC_HELM.read_text()
    )
    if not sa:
        failures.append("ALB controller ServiceAccount not found in workload/platform helm.yaml")
    else:
        associations = sorted(REPO_ROOT.glob(ASSOCIATION_GLOB))
        if not associations:
            failures.append(f"no associations.yaml found under {ASSOCIATION_GLOB}")
        for assoc in associations:
            if f"serviceAccount: {sa.group(1)}" not in assoc.read_text():
                failures.append(
                    f"{assoc.relative_to(REPO_ROOT)}: missing serviceAccount {sa.group(1)} "
                    "(contract with workload/platform/aws-load-balancer-controller/helm.yaml)"
                )

    sync_roots = sorted((REPO_ROOT / "workload").glob("eu-north-1-*"))
    for root in sync_roots:
        ks_file = root / "flux-ks.yaml"
        if not (root / "kustomization.yaml").is_file():
            failures.append(f"workload/{root.name}: missing kustomization.yaml")
        if not ks_file.is_file():
            failures.append(f"workload/{root.name}: missing flux-ks.yaml")
            continue
        # Read the order from the file, not the render: kustomize sorts
        # documents of one kind, so the render order is not the authored order.
        docs = [d for d in ks_file.read_text().split("\n---") if "kind: Kustomization" in d]
        names = []
        for d in docs:
            match = re.search(r"^\s+name: (\S+)", d, re.M)
            names.append(match.group(1) if match else None)
        if names != EXPECTED_KS:
            failures.append(f"workload/{root.name}/flux-ks.yaml: order {names} != {EXPECTED_KS}")
        render = subprocess.run(
            ["kubectl", "kustomize", str(root)], capture_output=True, text=True, check=False
        )
        if render.returncode != 0:
            failures.append(f"kubectl kustomize workload/{root.name} failed: {render.stderr[-300:]}")
        elif set(re.findall(r"^  name: (\S+)", render.stdout, re.M)) != set(EXPECTED_KS):
            failures.append(f"kubectl kustomize workload/{root.name} does not render {EXPECTED_KS}")

    instance_text = FLUX_INSTANCE.read_text()
    built_envs = sorted(
        d.name for d in (REPO_ROOT / "workload" / "environments").iterdir()
        if d.is_dir() and (d / "kustomization.yaml").is_file()
    )
    for env in built_envs:
        if not (REPO_ROOT / "workload" / f"eu-north-1-{env}").is_dir():
            failures.append(f"workload/environments/{env}: no workload/eu-north-1-{env} sync root")
        if f"  name: flux-instance-{env}\n" not in instance_text:
            failures.append(f"flux-instance.yaml: no flux-instance-{env} ConfigMap")
    # Reverse direction: every flux-instance-<env> ConfigMap needs a built
    # overlay; a ConfigMap for an unbuilt environment is dead management state.
    for env in sorted(set(re.findall(r"^  name: flux-instance-([a-z0-9-]+)$", instance_text, re.M))):
        if env not in built_envs:
            failures.append(f"flux-instance.yaml: flux-instance-{env} has no built workload/environments/{env}")

    rendered = {}
    for rel in FLUX_ROOTS:
        render = subprocess.run(
            ["kubectl", "kustomize", str(REPO_ROOT / rel)], capture_output=True, text=True, check=False
        )
        if render.returncode != 0:
            failures.append(f"kubectl kustomize {rel} failed: {render.stderr[-300:]}")
            continue
        rendered[rel] = rendered_objects(render.stdout)
    for i, a in enumerate(rendered):
        for b in list(rendered)[i + 1 :]:
            overlap = rendered[a] & rendered[b]
            if overlap:
                failures.append(f"{a} and {b} both render {sorted(overlap)}")

    if failures:
        print("workload overlay cross-check FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"OK: workload overlays use {len(used)} cluster-vars keys, sync root ordered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
