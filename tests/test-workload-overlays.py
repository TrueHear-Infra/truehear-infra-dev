#!/usr/bin/env python3
"""Cross-check the workload overlays' composition invariants.

- workload/base Flux Kustomizations name a GitRepository (AWS/Azure/GCP
  sync), and the rendered workload/local-host overlay names only
  OCIRepository sources (local registry sync). A missed patch reconciles
  nothing on one side or the other and Flux's status is the only signal.
- Every ${VAR} under workload/base carries a := default, so a cluster that
  ships no cluster-vars ConfigMap (local-host) still renders a full manifest
  instead of empty strings.
- Every workload/**/*.sops.yaml is encrypted to the .sops.yaml recipient
  (a plaintext Secret would otherwise be committed unnoticed).
Renders with `kubectl kustomize` or `kustomize build` (KUSTOMIZE env var).
"""

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
KUSTOMIZE = shlex.split(os.environ.get("KUSTOMIZE", "kubectl kustomize"))
FLUX_KS_HEADER = re.compile(r"apiVersion: kustomize\.toolkit\.fluxcd\.io/v1\nkind: Kustomization\n")


def render(path: Path) -> str:
    result = subprocess.run([*KUSTOMIZE, str(path)], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(f"render {path} failed: {result.stderr}")
    return result.stdout


def source_kinds(rendered: str) -> list[tuple[str, str]]:
    """(name, sourceRef.kind) for every Flux Kustomization in a rendered stream."""
    pairs = []
    for doc in rendered.split("\n---\n"):
        if not FLUX_KS_HEADER.search(doc):
            continue
        name = re.search(r"^metadata:\n(?:  .*\n)*?  name: (\S+)", doc, re.M)
        kind = re.search(r"sourceRef:\n\s+kind: (\S+)", doc)
        if name and kind:
            pairs.append((name.group(1), kind.group(1)))
    return pairs


def main() -> int:
    failures = []

    base = render(REPO_ROOT / "workload/base")
    base_kinds = source_kinds(base)
    if not base_kinds:
        failures.append("workload/base renders no Flux Kustomization")
    for name, kind in base_kinds:
        if kind != "GitRepository":
            failures.append(f"workload/base Kustomization {name} sourceRef.kind is {kind}, expected GitRepository")

    local = render(REPO_ROOT / "workload/local-host")
    local_kinds = source_kinds(local)
    for name, kind in local_kinds:
        if kind != "OCIRepository":
            failures.append(f"workload/local-host Kustomization {name} sourceRef.kind is {kind}, expected OCIRepository")
    missing = {n for n, _ in base_kinds} - {n for n, _ in local_kinds}
    if missing:
        failures.append(f"workload/local-host does not render base Kustomizations: {sorted(missing)}")

    for path in sorted((REPO_ROOT / "workload/base").rglob("*.yaml")):
        if path.name.endswith(".sops.yaml"):
            continue
        for var in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)([^}]*)\}", path.read_text()):
            if not var[1].startswith(":="):
                failures.append(f"{path.relative_to(REPO_ROOT)}: ${{{var[0]}}} has no := default")

    recipient = re.search(r"^\s+age: (age1\S+)", (REPO_ROOT / ".sops.yaml").read_text(), re.M).group(1)
    for path in sorted((REPO_ROOT / "workload").rglob("*.sops.yaml")):
        text = path.read_text()
        rel = path.relative_to(REPO_ROOT)
        if "ENC[AES256_GCM" not in text:
            failures.append(f"{rel}: not SOPS-encrypted")
        if f"recipient: {recipient}" not in text:
            failures.append(f"{rel}: not encrypted to the .sops.yaml recipient")
        if re.search(r"^(data|stringData):\n(?:  \S+: (?!ENC\[)\S)", text, re.M):
            failures.append(f"{rel}: plaintext value under data/stringData")

    if failures:
        print("workload overlay check FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(f"workload overlay check OK ({len(base_kinds)} base Kustomizations, "
          f"{len(local_kinds)} rendered by local-host, {len(list((REPO_ROOT / 'workload').rglob('*.sops.yaml')))} encrypted secrets)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
