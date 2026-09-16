#!/usr/bin/env python3
"""Cross-check the GCP workload-identity chain (issue #72).

The management-side WIF resources and the workload-cluster Config Connector
are coupled by literal names that no renderer validates. Requires PyYAML and
tomllib (mise's python provides both via `uv run`).
"""

import json
import re
import sys
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
IDENTITY = REPO_ROOT / "mgmt/gcp/infrastructure/kcc-identity"
MGMT_KCC = REPO_ROOT / "mgmt/gcp/infrastructure/kcc"
MGMT_CLUSTER = REPO_ROOT / "mgmt/gcp/clusters/europe-north1/management/cluster.yaml"
MISE_GCP = REPO_ROOT / "mise.gcp.toml"
BOOTSTRAP = REPO_ROOT / "bootstrap.toml"
KCC_WORKLOAD = REPO_ROOT / "workload/gcp-base/kcc/configconnector.yaml"
READER = REPO_ROOT / "workload/gcp-base/iam/reader.yaml"
KCC_WORKLOAD_SA = "cnrm-system/cnrm-controller-manager"

# The service accounts the workload-identity pool admits. CAPG exchanges on
# kind (provider `kind`) and post-pivot (provider `mgmt`); the management-side
# KCC does the same under the same krops-capg GSA.
EXPECTED_SUBJECTS = {
    "system:serviceaccount:capg-system:capg-manager",
    "system:serviceaccount:cnrm-system:cnrm-controller-manager",
}

# secret path -> (expected name, expected namespace, expected JSON key)
CREDENTIALS = {
    REPO_ROOT / "mgmt/gcp/capi-providers/capg-system/capg-wif-credentials.yaml": (
        "capg-wif-credentials", "capg-system", "credentials.json"),
    REPO_ROOT / "mgmt/gcp/infrastructure/kcc/kcc-wif-credentials.yaml": (
        "kcc-wif-credentials", "cnrm-system", "key.json"),
}


def docs(path: Path):
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def all_docs(root: Path):
    for path in sorted(root.rglob("*.yaml")):
        if path.name == "kustomization.yaml":
            continue
        for doc in docs(path):
            yield path.relative_to(REPO_ROOT), doc


def subject_set(cond: str):
    return set(re.findall(r"system:serviceaccount:[\w.-]+:[\w.-]+", cond))


def main() -> int:
    failures = []

    # ── (a) admitted subjects are consistent across Git provider, the
    # mise.gcp.toml wif-federate COND, and the krops-capg WIF grants. ──────
    mgmt_provider = None
    for _, doc in all_docs(IDENTITY):
        if doc.get("kind") == "IAMWorkloadIdentityPoolProvider" and doc["metadata"]["name"] == "mgmt":
            mgmt_provider = doc
    if mgmt_provider is None:
        failures.append(f"{IDENTITY.relative_to(REPO_ROOT)}: missing IAMWorkloadIdentityPoolProvider `mgmt`")
        admitted_git = set()
    else:
        admitted_git = subject_set(mgmt_provider["spec"]["attributeCondition"])

    mise_cond = re.search(r"COND='assertion\.sub in \[(.*)\]'", MISE_GCP.read_text(), re.S)
    admitted_mise = subject_set(mise_cond.group(1)) if mise_cond else set()
    if not mise_cond:
        failures.append("mise.gcp.toml: wif-federate COND not found")

    wif_subjects = set()
    for path, doc in all_docs(IDENTITY):
        if doc.get("kind") != "IAMPolicyMember":
            continue
        if doc["spec"].get("role") != "roles/iam.workloadIdentityUser":
            continue
        res = doc["spec"]["resourceRef"]
        if res.get("kind") != "IAMServiceAccount":
            failures.append(f"{path}: workloadIdentityUser grant on the wrong resource kind: {res}")
            continue
        if res.get("name") == "krops-kcc":
            continue  # the workload-cluster binding, verified in (e)
        if res.get("name") != "krops-capg":
            failures.append(f"{path}: workloadIdentityUser grant on the wrong service account: {res}")
            continue
        m = re.search(r"workloadIdentityPools/krops/subject/(system:serviceaccount:[\w.-]+:[\w.-]+)$", doc["spec"]["member"])
        if not m:
            failures.append(f"{path}: WIF grant member is not a krops-pool principal: {doc['spec']['member']}")
            continue
        wif_subjects.add(m.group(1))

    for label, got in [("Git mgmt provider", admitted_git), ("mise.gcp.toml COND", admitted_mise),
                       ("krops-capg WIF grants", wif_subjects)]:
        if got != EXPECTED_SUBJECTS:
            failures.append(f"(a) {label} subjects {sorted(got)} != {sorted(EXPECTED_SUBJECTS)}")

    # ── (b) the mgmt provider's issuer URL matches the management cluster,
    # and allowedAudiences is exactly [issuerUri]. ─────────────────────────
    mgmt_cp = [d for d in docs(MGMT_CLUSTER) if d.get("kind") == "GCPManagedControlPlane"]
    if not mgmt_cp:
        failures.append(f"{MGMT_CLUSTER.relative_to(REPO_ROOT)}: missing GCPManagedControlPlane")
    else:
        cluster_name = mgmt_cp[0]["spec"]["clusterName"]
        if mgmt_provider is not None:
            issuer = mgmt_provider["spec"]["oidc"]["issuerUri"]
            if f"locations/${{GCP_ZONE}}/clusters/{cluster_name}" not in issuer:
                failures.append(f"(b) mgmt provider issuerUri does not embed clusters/{cluster_name}: {issuer}")
            if mgmt_provider["spec"]["oidc"].get("allowedAudiences") != [issuer]:
                failures.append(f"(b) allowedAudiences must be [issuerUri], got {mgmt_provider['spec']['oidc'].get('allowedAudiences')}")

    # ── (c) both credential Secrets parse as external_account pointing at
    # pool krops, provider ${GCP_WIF_PROVIDER:=mgmt}, GSA krops-capg, and the
    # projected in-cluster token file. ──────────────────────────────────────
    for path, (secret_name, ns, key) in CREDENTIALS.items():
        rel = path.relative_to(REPO_ROOT)
        doc = docs(path)[0]
        if doc["metadata"]["name"] != secret_name or doc["metadata"]["namespace"] != ns:
            failures.append(f"(c) {rel}: secret must be {ns}/{secret_name}")
            continue
        raw = doc["stringData"][key]
        # placeholder stubbing: replace ${VAR} / ${VAR:=d} so the JSON parses.
        stubbed = re.sub(r"\$\{[\w]+(?::=[^}]+)?\}", "STUB", raw)
        try:
            cfg = json.loads(stubbed)
        except json.JSONDecodeError as e:
            failures.append(f"(c) {rel}: {key} does not parse after placeholder stubbing: {e}")
            continue
        if cfg.get("type") != "external_account":
            failures.append(f"(c) {rel}: type must be external_account, got {cfg.get('type')}")
        if "workloadIdentityPools/krops/providers/${GCP_WIF_PROVIDER:=mgmt}" not in raw:
            failures.append(f"(c) {rel}: audience must reference pool krops / provider ${{GCP_WIF_PROVIDER:=mgmt}}")
        if "krops-capg@${GCP_PROJECT}.iam.gserviceaccount.com" not in raw:
            failures.append(f"(c) {rel}: must impersonate krops-capg@${{GCP_PROJECT}}")
        if cfg.get("credential_source", {}).get("file") != "/var/run/secrets/kubernetes.io/serviceaccount/token":
            failures.append(f"(c) {rel}: credential_source.file must be the projected serviceaccount token")

    # ── (d) bootstrap.toml pins GCP_WIF_PROVIDER=mgmt on the pivot and
    # applies both credential Secrets to the pivot target. ─────────────────
    boot = tomllib.loads(BOOTSTRAP.read_text())
    gcp_env = boot.get("environments", {}).get("gcp")
    if gcp_env is None:
        failures.append("(d) bootstrap.toml: missing [environments.gcp]")
    else:
        if gcp_env.get("pivot-manifest-vars", {}).get("GCP_WIF_PROVIDER") != "mgmt":
            failures.append("(d) bootstrap.toml: [environments.gcp.pivot-manifest-vars] GCP_WIF_PROVIDER must be \"mgmt\"")
        pivot_manifests = set(gcp_env.get("pivot-manifests", []))
        for wanted in ["mgmt/gcp/capi-providers/capg-system/capg-wif-credentials.yaml",
                       "mgmt/gcp/infrastructure/kcc/kcc-wif-credentials.yaml"]:
            if wanted not in pivot_manifests:
                failures.append(f"(d) bootstrap.toml: {wanted} missing from [environments.gcp] pivot-manifests")

    # ── (e) the workload ConfigConnector names krops-kcc and the per-cluster
    # binding grants workloadIdentityUser on krops-kcc for the workload SA. ──
    cc = [d for d in docs(KCC_WORKLOAD) if d.get("kind") == "ConfigConnector"]
    if not cc:
        failures.append(f"{KCC_WORKLOAD.relative_to(REPO_ROOT)}: missing ConfigConnector")
    elif cc[0]["spec"].get("googleServiceAccount") != "krops-kcc@${GCP_PROJECT}.iam.gserviceaccount.com":
        failures.append(f"(e) {KCC_WORKLOAD.relative_to(REPO_ROOT)}: googleServiceAccount must be krops-kcc@${{GCP_PROJECT}}.iam.gserviceaccount.com")
    wi = [d for d in docs(IDENTITY / "europe-north1-workload.yaml") if d.get("kind") == "IAMPolicyMember"]
    if len(wi) != 1:
        failures.append(f"(e) {IDENTITY.relative_to(REPO_ROOT)}: expected 1 workload-identity binding, got {len(wi)}")
    else:
        b = wi[0]["spec"]
        if b.get("resourceRef", {}).get("name") != "krops-kcc" or b.get("role") != "roles/iam.workloadIdentityUser":
            failures.append(f"(e) {IDENTITY.relative_to(REPO_ROOT)}: binding must grant workloadIdentityUser on krops-kcc")
        if b.get("member") != f"serviceAccount:${{GCP_PROJECT}}.svc.id.goog[{KCC_WORKLOAD_SA}]":
            failures.append(f"(e) {IDENTITY.relative_to(REPO_ROOT)}: binding member must target {KCC_WORKLOAD_SA}")

    # ── (f) the management ConfigConnector reads kcc-wif-credentials, which
    # lives in cnrm-system. ─────────────────────────────────────────────────
    mcc = [d for d in docs(MGMT_KCC / "configconnector.yaml") if d.get("kind") == "ConfigConnector"]
    if not mcc:
        failures.append(f"{MGMT_KCC.relative_to(REPO_ROOT)}/configconnector.yaml: missing ConfigConnector")
    elif mcc[0]["spec"].get("credentialSecretName") != "kcc-wif-credentials":
        failures.append(f"(f) {MGMT_KCC.relative_to(REPO_ROOT)}/configconnector.yaml: credentialSecretName must be kcc-wif-credentials")
    kcc_sec = docs(MGMT_KCC / "kcc-wif-credentials.yaml")[0]
    if kcc_sec["metadata"]["namespace"] != "cnrm-system":
        failures.append(f"(f) {MGMT_KCC.relative_to(REPO_ROOT)}/kcc-wif-credentials.yaml: namespace must be cnrm-system")

    # ── the human reader's objectViewer grant targets the per-cluster GSA. ──
    reader_grants = [d for d in docs(READER) if d.get("kind") == "IAMPolicyMember" and d["spec"].get("role") == "roles/storage.objectViewer"]
    if len(reader_grants) != 1:
        failures.append(f"{READER.relative_to(REPO_ROOT)}: expected 1 storage.objectViewer grant, got {len(reader_grants)}")
    elif "krops-${CLUSTER_NAME}-reader" not in reader_grants[0]["spec"]["member"]:
        failures.append(f"{READER.relative_to(REPO_ROOT)}: objectViewer grant must reference the per-cluster reader GSA")

    if failures:
        print("gcp identity chain FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"gcp identity chain OK ({len(EXPECTED_SUBJECTS)} pool subjects, "
          f"{len(wif_subjects)} WIF grants, {len(CREDENTIALS)} credential secrets)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
