#!/usr/bin/env python3
"""The air-gap config artifact must carry only podinfo on the workload side.

workload/local-host composes workload/base (TrueHear apps pulling public
images) since the TrueHear integration; the offline bundle pre-loads no
such images. build-config-artifact.sh must rewrite the artifact copy of
workload/local-host/kustomization.yaml to the podinfo-only shape and must
not need workload/base at all. Exercises the strip function on a fixture.
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "airgap/scripts/build-config-artifact.sh"
SOURCE = REPO_ROOT / "workload/local-host/kustomization.yaml"


def function_body(name: str) -> str:
    text = SCRIPT.read_text()
    match = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}", text, re.M | re.S)
    if match is None:
        sys.exit(f"{SCRIPT}: function {name} not found")
    return f"{name}() {{\n{match.group(1)}}}\n"


def main() -> int:
    body = function_body("strip_workload_base")
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "kustomization.yaml"
        target.write_text(SOURCE.read_text())
        result = subprocess.run(["bash", "-euo", "pipefail", "-c", f"{body}\nstrip_workload_base {target}"],
                                capture_output=True, text=True, check=False)
        if result.returncode != 0:
            sys.exit(f"strip_workload_base failed: {result.stderr}")
        out = target.read_text()
    if "../base" in out or "local-path-storage" in out or "patches:" in out:
        sys.exit(f"artifact kustomization still references base components:\n{out}")
    if "- podinfo" not in out:
        sys.exit(f"artifact kustomization lost podinfo:\n{out}")
    print("air-gap workload overlay strip OK: podinfo only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
