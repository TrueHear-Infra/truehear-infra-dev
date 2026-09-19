#!/usr/bin/env python3
"""Require airgap/images.txt's k8s component pins to match what kubeadm deploys.

airgap/images.txt's kube-apiserver/controller-manager/proxy/scheduler,
coredns, etcd, and pause pins are meant to track what kubeadm actually
deploys for the Kubernetes version this environment runs (the kindest/node
tag), not just "latest upstream tag" (issue #142, items 3-4). This gate runs
the real kubeadm binary for that exact version (`kubeadm config images list
--kubernetes-version vX.Y.Z`) and fails if any pinned tag disagrees, printing
kubeadm's expected tag next to the offending pin.

kubeadm has no macOS build, so on a non-Linux host this runs it inside a
Linux container instead of requiring a local Linux toolchain; the project
already assumes a container engine is available (docs/operations.md
prerequisites).
"""

import platform
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
NODE_IMAGE_SOURCE = REPO_ROOT / "mgmt/local-host/clusters/docker/cluster.yaml"
MANAGEMENT_NODE_IMAGE_SOURCE = REPO_ROOT / "mgmt/local-host/clusters/management/cluster.yaml"
IMAGES_TXT = REPO_ROOT / "airgap/images.txt"

# depName, as both Renovate (images.txt) and kubeadm's own image list spell it.
TRACKED_COMPONENTS = {
    "registry.k8s.io/kube-apiserver",
    "registry.k8s.io/kube-controller-manager",
    "registry.k8s.io/kube-proxy",
    "registry.k8s.io/kube-scheduler",
    "registry.k8s.io/coredns/coredns",
    "registry.k8s.io/etcd",
    "registry.k8s.io/pause",
}

IMAGE_REF = re.compile(
    r"(?P<name>(?:[a-z0-9][a-z0-9.-]*(?::[0-9]+)?/)?(?:[a-z0-9][a-z0-9._-]*/)*"
    r"[a-z0-9][a-z0-9._-]*)"
    r":(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]*)"
    r"(?:@sha256:[a-f0-9]{64})?"
)


def _topology_version(path: Path) -> str:
    text = path.read_text()
    match = re.search(r"^\s*version: v(?P<version>\d+\.\d+\.\d+)\s*$", text, re.MULTILINE)
    if match is None:
        sys.exit(f"{path}: could not find a 'version: vX.Y.Z' topology pin")
    return match.group("version")


def target_kubernetes_version() -> str:
    """The Kubernetes version this environment runs, from the kindest/node pin.

    The workload and management clusters must agree, since images.txt serves both.
    """
    workload = _topology_version(NODE_IMAGE_SOURCE)
    management = _topology_version(MANAGEMENT_NODE_IMAGE_SOURCE)
    if workload != management:
        sys.exit(
            f"topology version mismatch: {NODE_IMAGE_SOURCE} pins v{workload} "
            f"but {MANAGEMENT_NODE_IMAGE_SOURCE} pins v{management}"
        )
    return workload


def _kubeadm_script(version: str, arch: str) -> str:
    return (
        "set -eu\n"
        f"curl -fsSL -o /usr/local/bin/kubeadm "
        f"https://dl.k8s.io/release/v{version}/bin/linux/{arch}/kubeadm\n"
        "chmod +x /usr/local/bin/kubeadm\n"
        f"kubeadm config images list --kubernetes-version v{version}\n"
    )


def kubeadm_expected_images(version: str) -> dict:
    """{kubeadm image name: tag} via the real kubeadm binary for this version."""
    if platform.system() == "Linux":
        arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), "amd64")
        completed = subprocess.run(
            ["bash", "-c", _kubeadm_script(version, arch)],
            check=True, text=True, capture_output=True,
        )
    else:
        setup = (
            "apt-get update -qq && apt-get install -y -qq curl ca-certificates "
            ">/dev/null 2>&1 && "
        )
        completed = subprocess.run(
            [
                "docker", "run", "--rm", "--platform", "linux/amd64",
                "debian:bookworm-slim", "bash", "-c",
                setup + _kubeadm_script(version, "amd64"),
            ],
            check=True, text=True, capture_output=True,
        )

    expected = {}
    for line in completed.stdout.splitlines():
        match = IMAGE_REF.fullmatch(line.strip())
        if match is not None:
            expected[match.group("name")] = match.group("tag")
    return expected


def images_txt_pins() -> dict:
    """{depName: {(tag, line_number), ...}} for every tracked component pin."""
    pins = {}
    for number, line in enumerate(IMAGES_TXT.read_text().splitlines(), 1):
        code = line.strip()
        if not code or code.startswith("#"):
            continue
        match = IMAGE_REF.fullmatch(code)
        if match is None or match.group("name") not in TRACKED_COMPONENTS:
            continue
        pins.setdefault(match.group("name"), set()).add((match.group("tag"), number))
    return pins


def main() -> int:
    version = target_kubernetes_version()
    print(f"target Kubernetes version (kindest/node): v{version}")

    try:
        expected = kubeadm_expected_images(version)
    except subprocess.CalledProcessError as exc:
        print(
            f"could not determine kubeadm's expected images: {exc}\n{exc.stderr}",
            file=sys.stderr,
        )
        return 1

    missing_expected = TRACKED_COMPONENTS - set(expected)
    if missing_expected:
        sys.exit(
            "kubeadm config images list did not report: "
            + ", ".join(sorted(missing_expected))
        )

    pins = images_txt_pins()
    failures = []
    for dep_name in sorted(TRACKED_COMPONENTS):
        expected_tag = expected[dep_name]
        occurrences = pins.get(dep_name, set())
        if not occurrences:
            failures.append(f"{dep_name}: not pinned in {IMAGES_TXT.name}")
            continue
        for tag, number in sorted(occurrences, key=lambda pair: pair[1]):
            if tag != expected_tag:
                failures.append(
                    f"{IMAGES_TXT.name}:{number}: {dep_name}:{tag} does not match "
                    f"what kubeadm v{version} deploys ({dep_name}:{expected_tag})"
                )

    if failures:
        print("Air-gap kubeadm image version check FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(
        f"Air-gap kubeadm image version check OK "
        f"({len(TRACKED_COMPONENTS)} components checked against kubeadm v{version})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
