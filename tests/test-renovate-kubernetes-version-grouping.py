#!/usr/bin/env python3
"""Offline unit test of the kubernetes-version group's boundaries (#142)."""

import unittest

from renovate_harness import apply_package_rules


class KubernetesVersionGroupingTest(unittest.TestCase):
    def test_only_k8s_release_version_pins_join_group(self):
        cases = [
            # kindest/node: local-host node image + topology.version pins.
            ("kindest/node", "docker", True),
            # kubectl (mise.toml) resolves to this depName.
            ("kubernetes/kubernetes", "github-releases", True),
            # kind CLI: its own release cadence, not a Kubernetes version.
            ("kubernetes-sigs/kind", "github-releases", False),
            # CAPI/kubeadm providers: grouped separately under "cluster-api".
            ("kubernetes-sigs/cluster-api", "github-releases", False),
            # retired local-talos pin: must still not join
            ("siderolabs/talos", "github-releases", False),
            # CAPI-family image, same registry.k8s.io host, must NOT join.
            ("registry.k8s.io/cluster-api/cluster-api-controller", "docker", False),
        ]
        dependencies = [
            {
                "depName": name, "packageName": name,
                "datasource": datasource, "manager": "custom.regex",
                "packageFile": "mise.toml",
            }
            for name, datasource, _ in cases
        ]
        results = apply_package_rules(dependencies)
        self.assertEqual(len(results), len(cases))
        for case, result in zip(cases, results):
            with self.subTest(dependency=case[:2]):
                self.assertEqual(result["groupName"] == "kubernetes-version", case[2])

    def test_group_does_not_split_major_minor(self):
        # One PR at the newest version: no rule may force separate major/minor
        # PRs for group members (the old kindest/node isolation rule did).
        results = apply_package_rules([
            {
                "depName": "kindest/node", "packageName": "kindest/node",
                "datasource": "docker", "manager": "custom.regex",
                "packageFile": "mise.toml",
            }
        ])
        self.assertFalse(results[0]["separateMajorMinor"])


if __name__ == "__main__":
    unittest.main()
