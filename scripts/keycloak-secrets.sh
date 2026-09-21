#!/usr/bin/env bash
# keycloak-secrets.sh – generate the four Keycloak Secrets the TrueHear
# manifests (workload/base/keycloak/) expect, SOPS-encrypted to the recipient
# in .sops.yaml. Upstream creates these out of band from its pki/ tree; here
# they are produced once per repository (or per rotation) and committed
# encrypted. Nothing plaintext touches the working tree: every file is
# written already encrypted from a temp dir that is removed on exit.
#
# Usage: scripts/keycloak-secrets.sh [--force] [<output-dir>]
#   <output-dir> defaults to workload/base/keycloak. Existing *.sops.yaml
#   there abort the run unless --force is given (rotation).
# Requires: openssl, sops (PATH; mise provides them). SOPS_AGE_KEY_FILE is
# NOT required: encryption uses only the public recipient in .sops.yaml.
set -euo pipefail

FORCE=0
OUT=workload/base/keycloak
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) OUT="$1" ;;
  esac
  shift
done

for cmd in openssl sops; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "ERROR: $cmd not found in PATH" >&2; exit 1; }
done
[ -d "$OUT" ] || { echo "ERROR: output directory does not exist: $OUT" >&2; exit 1; }

for name in keycloak-postgresql-credentials keycloak-bootstrap-admin keycloak-postgresql-tls keycloak-tls; do
  if [ -e "$OUT/$name.sops.yaml" ] && [ "$FORCE" -ne 1 ]; then
    echo "ERROR: $OUT/$name.sops.yaml exists; pass --force to rotate" >&2
    exit 1
  fi
done

WORK=$(mktemp -d "${TMPDIR:-/tmp}/keycloak-secrets.XXXXXX")
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

PG_HOST=keycloak-postgresql.keycloak.svc.cluster.local
KC_HOST=keycloak.keycloak.svc.cluster.local
DAYS=825

rand() { openssl rand -base64 33 | tr -d '/+=' | cut -c1-32; }

# ── PKI: one throwaway CA, two server leaves ─────────────────────────────────
openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days "$DAYS" \
  -subj "/CN=truehear-platform-ca" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout "$WORK/ca.key" -out "$WORK/ca.crt" 2>/dev/null

leaf() { # leaf <basename> <host> <extra-san-csv>
  openssl req -newkey rsa:2048 -nodes -sha256 -subj "/CN=$2" \
    -keyout "$WORK/$1.key" -out "$WORK/$1.csr" 2>/dev/null
  printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\nkeyUsage=digitalSignature,keyEncipherment\n' "$3" > "$WORK/$1.ext"
  openssl x509 -req -sha256 -days "$DAYS" -in "$WORK/$1.csr" \
    -CA "$WORK/ca.crt" -CAkey "$WORK/ca.key" -CAcreateserial \
    -extfile "$WORK/$1.ext" -out "$WORK/$1.crt" 2>/dev/null
}
leaf pg "$PG_HOST" "DNS:$PG_HOST,DNS:keycloak-postgresql.keycloak.svc,DNS:keycloak-postgresql,DNS:keycloak-postgresql-internal.keycloak.svc.cluster.local"
leaf kc "$KC_HOST" "DNS:$KC_HOST,DNS:keycloak.keycloak.svc,DNS:keycloak,DNS:localhost,IP:127.0.0.1"

indent() { sed 's/^/    /' "$1"; }

# ── Manifests (plaintext only inside $WORK) ─────────────────────────────────
PG_PASS=$(rand)
ADMIN_PASS=$(rand)

cat > "$WORK/keycloak-postgresql-credentials.sops.yaml" <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: keycloak-postgresql-credentials
  namespace: keycloak
  labels:
    app.kubernetes.io/name: keycloak-postgresql
    app.kubernetes.io/component: database
    app.kubernetes.io/part-of: truehear-platform
type: Opaque
stringData:
  database: keycloak
  username: keycloak
  password: $PG_PASS
EOF

cat > "$WORK/keycloak-bootstrap-admin.sops.yaml" <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: keycloak-bootstrap-admin
  namespace: keycloak
  labels:
    app.kubernetes.io/name: keycloak
    app.kubernetes.io/component: identity-provider
    app.kubernetes.io/part-of: truehear-platform
type: Opaque
stringData:
  username: admin
  password: $ADMIN_PASS
EOF

{
  cat <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: keycloak-postgresql-tls
  namespace: keycloak
  labels:
    app.kubernetes.io/name: keycloak-postgresql
    app.kubernetes.io/component: database
    app.kubernetes.io/part-of: truehear-platform
type: kubernetes.io/tls
stringData:
  tls.crt: |
EOF
  indent "$WORK/pg.crt"
  echo "  tls.key: |"
  indent "$WORK/pg.key"
  echo "  ca.crt: |"
  indent "$WORK/ca.crt"
} > "$WORK/keycloak-postgresql-tls.sops.yaml"

{
  cat <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: keycloak-tls
  namespace: keycloak
  labels:
    app.kubernetes.io/name: keycloak
    app.kubernetes.io/component: identity-provider
    app.kubernetes.io/part-of: truehear-platform
type: kubernetes.io/tls
stringData:
  tls.crt: |
EOF
  indent "$WORK/kc.crt"
  echo "  tls.key: |"
  indent "$WORK/kc.key"
  echo "  ca.crt: |"
  indent "$WORK/ca.crt"
} > "$WORK/keycloak-tls.sops.yaml"

# ── Encrypt in $WORK (path must match .sops.yaml's path_regex), then move ───
# sops resolves creation rules against the path relative to the .sops.yaml
# it finds walking up from the file; stage under a matching relative path.
STAGE="$WORK/stage/$OUT"
mkdir -p "$STAGE"
for name in keycloak-postgresql-credentials keycloak-bootstrap-admin keycloak-postgresql-tls keycloak-tls; do
  cp "$WORK/$name.sops.yaml" "$STAGE/$name.sops.yaml"
  sops --config .sops.yaml --encrypt --in-place --input-type yaml --output-type yaml "$STAGE/$name.sops.yaml"
  grep -q 'ENC\[AES256_GCM' "$STAGE/$name.sops.yaml" || { echo "ERROR: $name.sops.yaml did not encrypt" >&2; exit 1; }
  mv "$STAGE/$name.sops.yaml" "$OUT/$name.sops.yaml"
  echo "wrote $OUT/$name.sops.yaml"
done

echo
echo "Keycloak bootstrap admin: username=admin (password is in $OUT/keycloak-bootstrap-admin.sops.yaml;"
echo "read it with: mise run sops-decrypt $OUT/keycloak-bootstrap-admin.sops.yaml)"
