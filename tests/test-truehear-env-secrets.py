#!/usr/bin/env python3
"""Exercise scripts/truehear-env-secrets.sh against a throwaway recipient.

The script is the only sanctioned way to produce the SOPS-encrypted Secrets
under workload/environments/<env>/. It must: write exactly the eleven files
the environment overlay needs, encrypt every stringData value (no plaintext
survives), issue TLS material whose SANs cover the in-cluster Service names
the manifests dial, refuse to overwrite existing files without --force, and
reject a bad --env with exit 2. Runs sops/age/openssl from PATH (mise).
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts/truehear-env-secrets.sh"
ENV = "staging"

# rel path under --out, namespace, name, k8s type
EXPECTED = [
    ("keycloak/keycloak-bootstrap-admin.sops.yaml", "keycloak", "keycloak-bootstrap-admin", "Opaque"),
    ("keycloak/keycloak-postgresql-credentials.sops.yaml", "keycloak", "keycloak-postgresql-credentials", "Opaque"),
    ("keycloak/keycloak-postgresql-tls.sops.yaml", "keycloak", "keycloak-postgresql-tls", "kubernetes.io/tls"),
    ("keycloak/keycloak-tls.sops.yaml", "keycloak", "keycloak-tls", "kubernetes.io/tls"),
    ("vault/vault-tls.sops.yaml", "vault", "vault-tls", "kubernetes.io/tls"),
    ("redis/redis-tls.sops.yaml", "redis", "redis-tls", "kubernetes.io/tls"),
    ("redis/redis-auth.sops.yaml", "redis", "redis-auth", "Opaque"),
    ("redis/redis-backend-acl.sops.yaml", "redis", "redis-backend-acl", "Opaque"),
    ("rabbitmq/rabbitmq-tls.sops.yaml", "rabbitmq", "rabbitmq-tls", "kubernetes.io/tls"),
    ("rabbitmq/rabbitmq-erlang-cookie.sops.yaml", "rabbitmq", "rabbitmq-erlang-cookie", "Opaque"),
    ("rabbitmq/rabbitmq-bootstrap-auth.sops.yaml", "rabbitmq", "rabbitmq-bootstrap-auth", "Opaque"),
]
TLS_KEYS = ["tls.crt", "tls.key", "ca.crt"]
OPAQUE_KEYS = {
    "keycloak-bootstrap-admin": ["username", "password"],
    "keycloak-postgresql-credentials": ["database", "username", "password"],
    "redis-auth": ["redis-auth.conf"],
    "redis-backend-acl": ["users.conf", "password"],
    "rabbitmq-erlang-cookie": ["erlang-cookie"],
    "rabbitmq-bootstrap-auth": ["username", "password"],
}
SAN = {
    "keycloak-postgresql-tls": (
        [
            "keycloak-postgresql.keycloak.svc.cluster.local",
            "keycloak-postgresql.keycloak.svc",
            "keycloak-postgresql",
            "keycloak-postgresql-internal.keycloak.svc.cluster.local",
        ],
        False,
    ),
    "keycloak-tls": (
        [
            "keycloak.keycloak.svc.cluster.local",
            "keycloak.keycloak.svc",
            "keycloak",
            "localhost",
        ],
        True,
    ),
    "vault-tls": (
        [
            "vault.vault.svc.cluster.local",
            "vault-internal.vault.svc.cluster.local",
            "vault-0.vault-internal.vault.svc.cluster.local",
            "vault-1.vault-internal.vault.svc.cluster.local",
            "vault-2.vault-internal.vault.svc.cluster.local",
            "*.vault-internal.vault.svc.cluster.local",
            "localhost",
        ],
        True,
    ),
    "redis-tls": (
        [
            "redis.redis.svc.cluster.local",
            "redis-headless.redis.svc.cluster.local",
            "redis-0.redis-headless.redis.svc.cluster.local",
            "redis-1.redis-headless.redis.svc.cluster.local",
            "redis-2.redis-headless.redis.svc.cluster.local",
            "redis-3.redis-headless.redis.svc.cluster.local",
            "redis-4.redis-headless.redis.svc.cluster.local",
            "redis-5.redis-headless.redis.svc.cluster.local",
            "*.redis-headless.redis.svc.cluster.local",
        ],
        False,
    ),
    "rabbitmq-tls": (
        [
            "rabbitmq.rabbitmq.svc.cluster.local",
            "rabbitmq-headless.rabbitmq.svc.cluster.local",
            "rabbitmq-0.rabbitmq-headless.rabbitmq.svc.cluster.local",
            "rabbitmq-1.rabbitmq-headless.rabbitmq.svc.cluster.local",
            "rabbitmq-2.rabbitmq-headless.rabbitmq.svc.cluster.local",
            "*.rabbitmq-headless.rabbitmq.svc.cluster.local",
            "localhost",
        ],
        False,
    ),
}
ACL_CMDS = (
    "~* +auth +hello +ping +client|setinfo +client|setname +cluster|slots "
    "+cluster|shards +asking +get +set +setex +del +expire +pexpire "
    "+incr +scan +decr +pttl +script|load +evalsha"
)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def block_of(yaml_text, key):
    """Return the lines of a `key: |` block, indentation stripped.

    sops --output-type yaml re-dumps with 4-space indentation, so the
    block's content indent cannot be assumed: measure the key's own indent
    and take the deeper-indented lines below it.
    """
    m = re.search(rf"^(\s*){re.escape(key)}: \|\n", yaml_text, re.M)
    if m is None:
        return None
    key_indent = len(m.group(1).expandtabs())
    lines = []
    for line in yaml_text[m.end():].splitlines():
        if not line.strip():
            continue
        if len(line) - len(line.lstrip(" ")) <= key_indent:
            break
        lines.append(line)
    if not lines:
        return None
    indent = len(lines[0]) - len(lines[0].lstrip(" "))
    return [l[indent:] for l in lines]


def scalar_of(yaml_text, key):
    m = re.search(rf"^\s*{re.escape(key)}: (\S+)\s*$", yaml_text, re.M)
    return m.group(1) if m else None


def main() -> int:
    for tool in ("sops", "age-keygen", "openssl"):
        if shutil.which(tool) is None:
            print(f"SKIP: {tool} not on PATH (run through mise)")
            return 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        key = tmp / "age.agekey"
        run(["age-keygen", "-o", str(key)])
        pub = run(["age-keygen", "-y", str(key)]).stdout.strip()
        out = tmp / "workload" / "environments" / ENV
        out.mkdir(parents=True)
        (tmp / ".sops.yaml").write_text(
            "creation_rules:\n"
            "  - path_regex: workload/.*\\.sops\\.yaml$\n"
            '    encrypted_regex: "^(data|stringData)$"\n'
            f"    age: {pub}\n"
        )
        env = {**os.environ, "SOPS_AGE_KEY_FILE": str(key)}
        args = ["--env", ENV, "--repo-root", str(tmp), "--out", str(out)]

        bad = run(["bash", str(SCRIPT), *args, "--env", "Staging"])
        if bad.returncode != 2:
            sys.exit(f"--env Staging must exit 2 (got {bad.returncode}):\n{bad.stdout}\n{bad.stderr}")
        if any(out.rglob("*.sops.yaml")):
            sys.exit("bad --env run must not write any file")

        first = run(["bash", str(SCRIPT), *args], cwd=tmp, env=env)
        if first.returncode != 0:
            sys.exit(f"first run failed (rc={first.returncode}):\n{first.stdout}\n{first.stderr}")
        got = {str(p.relative_to(out)) for p in out.rglob("*.sops.yaml")}
        want = {rel for rel, *_ in EXPECTED}
        if got != want:
            sys.exit(f"expected {sorted(want)}, got {sorted(got)}")

        def decrypted(rel):
            r = run(
                ["sops", "--decrypt", "--input-type", "yaml", "--output-type", "yaml",
                 str(out / rel)], cwd=tmp, env=env)
            if r.returncode != 0:
                sys.exit(f"decrypt {rel}: {r.stderr}")
            return r.stdout

        def san_text(pem):
            # -text -noout instead of -ext subjectAltName: the latter is
            # OpenSSL 3 only (macOS ships LibreSSL); -text prints the SAN
            # section on both, so the DNS: entries are greppable either way.
            r = run(["openssl", "x509", "-noout", "-text"], input=pem)
            if r.returncode != 0:
                sys.exit(f"openssl x509 failed: {r.stderr}")
            return r.stdout

        ca_blocks = []
        for rel, ns, name, ktype in EXPECTED:
            text = (out / rel).read_text()
            if "ENC[AES256_GCM" not in text:
                sys.exit(f"{rel}: not SOPS-encrypted")
            if f"recipient: {pub}" not in text:
                sys.exit(f"{rel}: not encrypted to the .sops.yaml recipient")
            plain = decrypted(rel)
            if f"  namespace: {ns}\n" not in plain and not re.search(
                    rf"^\s+namespace: {re.escape(ns)}\s*$", plain, re.M):
                sys.exit(f"{rel}: wrong or missing namespace {ns}")
            if not re.search(rf"^\s*name: {re.escape(name)}\s*$", plain, re.M):
                sys.exit(f"{rel}: wrong or missing name {name}")
            if not re.search(rf"^\s*type: {re.escape(ktype)}\s*$", plain, re.M):
                sys.exit(f"{rel}: wrong or missing type {ktype}")
            if ktype == "kubernetes.io/tls":
                keys = TLS_KEYS
                cert = block_of(plain, "tls.crt")
                if cert is None:
                    sys.exit(f"{rel}: tls.crt block missing or empty")
                pem = "\n".join(cert) + "\n"
                st = san_text(pem)
                dns_list, has_ip = SAN[name]
                if "X509v3 Subject Alternative Name" not in st:
                    sys.exit(f"{rel}: cert has no SAN section")
                for d in dns_list:
                    if f"DNS:{d}" not in st:
                        sys.exit(f"{rel}: cert SAN lacks {d}")
                if has_ip and "IP Address:127.0.0.1" not in st:
                    sys.exit(f"{rel}: cert SAN lacks IP:127.0.0.1")
                ca = block_of(plain, "ca.crt")
                if ca is None:
                    sys.exit(f"{rel}: ca.crt block missing or empty")
                ca_blocks.append("\n".join(ca))
            else:
                keys = OPAQUE_KEYS[name]
                for k in keys:
                    if not re.search(rf"^\s*{re.escape(k)}: ENC\[AES256_GCM", text, re.M):
                        sys.exit(f"{rel}: stringData value {k} is not encrypted")
                if name == "keycloak-bootstrap-admin":
                    if scalar_of(plain, "username") != "admin":
                        sys.exit(f"{rel}: username must be admin")
                if name == "keycloak-postgresql-credentials":
                    if scalar_of(plain, "database") != "keycloak":
                        sys.exit(f"{rel}: database must be keycloak")
                    if scalar_of(plain, "username") != "keycloak":
                        sys.exit(f"{rel}: username must be keycloak")
                if name == "rabbitmq-bootstrap-auth":
                    if scalar_of(plain, "username") != "truehear-admin":
                        sys.exit(f"{rel}: username must be truehear-admin")
                if name == "rabbitmq-erlang-cookie":
                    cookie = scalar_of(plain, "erlang-cookie")
                    if cookie is None or not re.fullmatch(r"[0-9a-f]{64}", cookie):
                        sys.exit(f"{rel}: erlang-cookie must be 32 random bytes as hex")
                if name == "redis-auth":
                    lines = block_of(plain, "redis-auth.conf")
                    if lines is None:
                        sys.exit(f"{rel}: redis-auth.conf block missing or empty")
                    if len(lines) != 2:
                        sys.exit(f"{rel}: redis-auth.conf must have exactly two lines: {lines}")
                    m1 = re.fullmatch(r"requirepass ([A-Za-z0-9_-]+)", lines[0])
                    m2 = re.fullmatch(r"masterauth ([A-Za-z0-9_-]+)", lines[1])
                    if not m1 or not m2:
                        sys.exit(f"{rel}: redis-auth.conf lines malformed: {lines}")
                    if m1.group(1) != m2.group(1):
                        sys.exit(f"{rel}: requirepass and masterauth must match")
                if name == "redis-backend-acl":
                    lines = block_of(plain, "users.conf")
                    if lines is None or len(lines) != 1:
                        sys.exit(f"{rel}: users.conf must have exactly one line")
                    pw = scalar_of(plain, "password")
                    if not pw:
                        sys.exit(f"{rel}: password key missing")
                    digest = hashlib.sha256(pw.encode()).hexdigest()
                    expected_line = f"user truehear-backend-{ENV} on #{digest} {ACL_CMDS}"
                    if lines[0] != expected_line:
                        sys.exit(f"{rel}: users.conf line mismatch:\n got: {lines[0]}\nwant: {expected_line}")
            # every stringData value must be encrypted in the on-disk file
            for k in keys:
                if not re.search(rf"^\s*{re.escape(k)}: ENC\[AES256_GCM", text, re.M):
                    sys.exit(f"{rel}: stringData value {k} is not encrypted")

        if len(set(ca_blocks)) != 1:
            sys.exit("ca.crt differs between TLS Secrets (one CA per run expected)")

        before = {str(p.relative_to(out)): (p.stat().st_mtime_ns,
                                            hashlib.sha256(p.read_bytes()).hexdigest())
                  for p in out.rglob("*.sops.yaml")}
        second = run(["bash", str(SCRIPT), *args], cwd=tmp, env=env)
        if second.returncode == 0:
            sys.exit("second run must refuse to overwrite without --force")
        after = {str(p.relative_to(out)): (p.stat().st_mtime_ns,
                                           hashlib.sha256(p.read_bytes()).hexdigest())
                 for p in out.rglob("*.sops.yaml")}
        if before != after:
            sys.exit("second run without --force changed existing files")

    print(f"OK: truehear-env-secrets writes 11 encrypted Secrets for {ENV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
