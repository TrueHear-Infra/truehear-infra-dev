#!/usr/bin/env python3
"""Offline unit test of the cert-manager group's boundaries using Renovate's engine.

See docs/dependencies.md#managed-surfaces for why cert-manager's chart pin
and its image tags must land in the same Renovate group.
"""

import unittest

from renovate_harness import apply_package_rules


class CertManagerGroupingTest(unittest.TestCase):
    def test_chart_and_images_join_the_same_group(self):
        cases = [
            ("cert-manager", "helm", "bootstrap.toml", True),
            ("cert-manager", "helm", "pivot.sh", True),
            ("cluster-api-operator", "helm", "bootstrap.toml", True),
            # guards: must not join by accident
            ("kube-prometheus-stack", "helm", "bootstrap.toml", False),
        ]
        dependencies = [
            {
                "depName": name, "packageName": name,
                "datasource": datasource, "packageFile": package_file,
            }
            for name, datasource, package_file, _ in cases
        ]
        results = apply_package_rules(dependencies)
        self.assertEqual(len(results), len(cases))
        for case, result in zip(cases, results):
            with self.subTest(dependency=case[:3]):
                self.assertEqual(result["groupName"] == "platform-charts", case[3])


if __name__ == "__main__":
    unittest.main()
