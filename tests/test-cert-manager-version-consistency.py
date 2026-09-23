#!/usr/bin/env python3
"""Cross-check cert-manager's chart version against every place it must match.

bootstrap.toml's charts.cert-manager pin is authoritative; the HelmRelease
manifests are cross-checked by test-bootstrap-config.py; this covers
pivot.sh's imperative install.
"""

import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_TOML = REPO_ROOT / "bootstrap.toml"
PIVOT_SH = REPO_ROOT / "pivot.sh"


def chart_version() -> str:
    config = tomllib.loads(BOOTSTRAP_TOML.read_text())
    version = config.get("charts", {}).get("cert-manager")
    if version is None:
        sys.exit(f"{BOOTSTRAP_TOML}: charts.cert-manager is missing")
    return version


def pivot_version() -> str:
    match = re.search(r'CERT_MANAGER_VERSION="([^"]+)"', PIVOT_SH.read_text())
    if match is None:
        sys.exit(f"{PIVOT_SH}: could not find CERT_MANAGER_VERSION=\"...\"")
    return match.group(1)


def main() -> int:
    expected = chart_version()
    pivot = pivot_version()
    if pivot != expected:
        print("cert-manager version consistency check FAILED:", file=sys.stderr)
        print(
            f"  - {PIVOT_SH}: CERT_MANAGER_VERSION={pivot!r} != bootstrap.toml's {expected!r}",
            file=sys.stderr,
        )
        return 1

    print(f"cert-manager version consistency check OK (pivot.sh v{expected})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
