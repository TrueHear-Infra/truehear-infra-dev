#!/usr/bin/env python3
"""Exercise scripts/keycloak-secrets.sh against a throwaway recipient.

The script is the only sanctioned way to produce the four Keycloak Secrets
under workload/base/keycloak/. It must: write exactly those four files,
encrypt every data/stringData value (no plaintext survives), issue TLS
material whose SANs match the in-cluster Service names the manifests dial
with sslmode=verify-full / KC_HOSTNAME strict, and refuse to overwrite
existing files without --force. Runs sops/age/openssl from PATH (mise).
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts/keycloak-secrets.sh"
EXPECTED_FILES = {
    "keycloak-postgresql-credentials.sops.yaml",
    "keycloak-bootstrap-admin.sops.yaml",
    "keycloak-postgresql-tls.sops.yaml",
    "keycloak-tls.sops.yaml",
}
PG_HOST = "keycloak-postgresql.keycloak.svc.cluster.local"
KC_HOST = "keycloak.keycloak.svc.cluster.local"


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def main() -> int:
    for tool in ("sops", "age-keygen", "openssl"):
        if shutil.which(tool) is None:
            sys.exit(f"{tool} not on PATH (run through mise)")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        key = tmp / "age.agekey"
        run(["age-keygen", "-o", str(key)])
        pub = run(["age-keygen", "-y", str(key)]).stdout.strip()
        out = tmp / "workload/base/keycloak"
        out.mkdir(parents=True)
        (tmp / ".sops.yaml").write_text(
            "creation_rules:\n"
            "  - path_regex: workload/.*\\.sops\\.yaml$\n"
            '    encrypted_regex: "^(data|stringData)$"\n'
            f"    age: {pub}\n"
        )
        env = {**os.environ, "SOPS_AGE_KEY_FILE": str(key)}

        first = run(["bash", str(SCRIPT), str(out)], cwd=tmp, env=env)
        if first.returncode != 0:
            sys.exit(f"first run failed:\n{first.stdout}\n{first.stderr}")
        got = {p.name for p in out.iterdir()}
        if got != EXPECTED_FILES:
            sys.exit(f"expected {sorted(EXPECTED_FILES)}, got {sorted(got)}")

        for name in EXPECTED_FILES:
            text = (out / name).read_text()
            if "ENC[AES256_GCM" not in text:
                sys.exit(f"{name}: not SOPS-encrypted")
            if re.search(r"^\s+(password|tls\.key|ca\.crt|tls\.crt|username|database): (?!ENC\[)", text, re.M):
                sys.exit(f"{name}: a data value is plaintext")
            if f"recipient: {pub}" not in text:
                sys.exit(f"{name}: not encrypted to the .sops.yaml recipient")

        # Decrypt and check the TLS SANs the manifests depend on.
        def decrypted(name):
            r = run(["sops", "--decrypt", "--input-type", "yaml", "--output-type", "yaml", str(out / name)], cwd=tmp, env=env)
            if r.returncode != 0:
                sys.exit(f"decrypt {name}: {r.stderr}")
            return r.stdout

        def san_of(yaml_text, host_key="tls.crt"):
            # sops --output-type yaml re-dumps with 4-space indentation, so
            # the block's content indent cannot be assumed: measure the
            # key's own indent and take the deeper-indented lines below it.
            m = re.search(rf"^(\s*){re.escape(host_key)}: \|\n", yaml_text, re.M)
            if m is None:
                sys.exit(f"{host_key} block not found")
            key_indent = len(m.group(1).expandtabs())
            lines = []
            for line in yaml_text[m.end():].splitlines():
                if not line.strip():
                    continue
                if len(line) - len(line.lstrip(" ")) <= key_indent:
                    break
                lines.append(line)
            if not lines:
                sys.exit(f"{host_key} block is empty")
            indent = len(lines[0]) - len(lines[0].lstrip(" "))
            pem = "\n".join(l[indent:] for l in lines) + "\n"
            # -text -noout instead of -ext subjectAltName: the latter is
            # OpenSSL 3 only (macOS ships LibreSSL); -text prints the SAN
            # section on both, so the DNS: entries are greppable either way.
            r = run(["openssl", "x509", "-noout", "-text"], input=pem)
            return r.stdout

        pg = decrypted("keycloak-postgresql-tls.sops.yaml")
        if PG_HOST not in san_of(pg):
            sys.exit(f"postgresql cert SAN lacks {PG_HOST}")
        if "ca.crt: |" not in pg:
            sys.exit("postgresql TLS secret lacks ca.crt (keycloak mounts it for verify-full)")
        kc = decrypted("keycloak-tls.sops.yaml")
        if KC_HOST not in san_of(kc):
            sys.exit(f"keycloak cert SAN lacks {KC_HOST}")
        creds = decrypted("keycloak-postgresql-credentials.sops.yaml")
        if "database: keycloak" not in creds or "username: keycloak" not in creds:
            sys.exit("postgresql credentials must use database=keycloak, username=keycloak (KC_DB_URL is fixed upstream)")
        admin = decrypted("keycloak-bootstrap-admin.sops.yaml")
        if "username: admin" not in admin:
            sys.exit("bootstrap admin username must be admin")

        second = run(["bash", str(SCRIPT), str(out)], cwd=tmp, env=env)
        if second.returncode == 0:
            sys.exit("second run must refuse to overwrite without --force")
        forced = run(["bash", str(SCRIPT), "--force", str(out)], cwd=tmp, env=env)
        if forced.returncode != 0:
            sys.exit(f"--force run failed: {forced.stderr}")

    print("keycloak-secrets.sh OK: four encrypted secrets, SANs match, overwrite guarded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
