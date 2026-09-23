#!/usr/bin/env bash
# truehear-env-secrets.sh -- generate every SOPS-encrypted Secret an
# environment overlay needs (workload/environments/<env>/), from generated
# material by default or operator-supplied PKIv2 leaves. One throwaway CA per
# run signs all leaves, so ca.crt is the same in every TLS Secret. Nothing
# plaintext touches the working tree: every file is written already
# encrypted from a temp dir that is removed on exit.
#
# Usage: scripts/truehear-env-secrets.sh --env <env> [--out DIR] [--repo-root DIR]
#       [--force] [--keycloak-tls DIR] [--postgresql-tls DIR] [--vault-tls DIR]
#       [--redis-tls DIR] [--rabbitmq-tls DIR]
#   <env> matches ^[a-z][a-z0-9-]*$; --out defaults to <repo-root>/workload/
#   environments/<env>; each --*-tls DIR must contain tls.crt, tls.key and
#   ca.crt (copied instead of generated). Existing target files abort the run
#   before anything is written unless --force is given (rotation).
# Requires: openssl, sops (PATH; mise provides them). SOPS_AGE_KEY_FILE is
# NOT required: encryption uses only the public recipient in .sops.yaml.
set -euo pipefail

ENV=""
FORCE=0
REPO_ROOT="$(pwd)"
OUT=""
KC_TLS=""
PG_TLS=""
VAULT_TLS=""
REDIS_TLS=""
RMQ_TLS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --env) [ $# -ge 2 ] || { echo "ERROR: --env needs a value" >&2; exit 2; }; ENV="$2"; shift 2 ;;
    --out) [ $# -ge 2 ] || { echo "ERROR: --out needs a value" >&2; exit 2; }; OUT="$2"; shift 2 ;;
    --repo-root) [ $# -ge 2 ] || { echo "ERROR: --repo-root needs a value" >&2; exit 2; }; REPO_ROOT="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --keycloak-tls) [ $# -ge 2 ] || { echo "ERROR: --keycloak-tls needs a value" >&2; exit 2; }; KC_TLS="$2"; shift 2 ;;
    --postgresql-tls) [ $# -ge 2 ] || { echo "ERROR: --postgresql-tls needs a value" >&2; exit 2; }; PG_TLS="$2"; shift 2 ;;
    --vault-tls) [ $# -ge 2 ] || { echo "ERROR: --vault-tls needs a value" >&2; exit 2; }; VAULT_TLS="$2"; shift 2 ;;
    --redis-tls) [ $# -ge 2 ] || { echo "ERROR: --redis-tls needs a value" >&2; exit 2; }; REDIS_TLS="$2"; shift 2 ;;
    --rabbitmq-tls) [ $# -ge 2 ] || { echo "ERROR: --rabbitmq-tls needs a value" >&2; exit 2; }; RMQ_TLS="$2"; shift 2 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$ENV" ]; then
  echo "ERROR: --env is required (usage: $0 --env <env> ...)" >&2
  exit 2
fi
if ! printf '%s' "$ENV" | grep -Eq '^[a-z][a-z0-9-]*$'; then
  echo "ERROR: --env must match ^[a-z][a-z0-9-]*$: $ENV" >&2
  exit 2
fi
[ -f "$REPO_ROOT/.sops.yaml" ] || { echo "ERROR: no .sops.yaml in repo root: $REPO_ROOT" >&2; exit 1; }
[ -z "$OUT" ] && OUT="$REPO_ROOT/workload/environments/$ENV"
mkdir -p "$OUT"

for cmd in openssl sops; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "ERROR: $cmd not found in PATH" >&2; exit 1; }
done

# Each --*-tls DIR must carry the three files the Secret expects.
tls_dir_ok() { # tls_dir_ok <label> <dir>
  local f
  [ -n "$2" ] || return 0
  for f in tls.crt tls.key ca.crt; do
    [ -f "$2/$f" ] || { echo "ERROR: --$1 DIR $2 lacks $f" >&2; exit 1; }
  done
}
tls_dir_ok keycloak-tls "$KC_TLS"
tls_dir_ok postgresql-tls "$PG_TLS"
tls_dir_ok vault-tls "$VAULT_TLS"
tls_dir_ok redis-tls "$REDIS_TLS"
tls_dir_ok rabbitmq-tls "$RMQ_TLS"

COMPONENTS="keycloak vault redis rabbitmq"
for c in $COMPONENTS; do
  mkdir -p "$OUT/$c"
done
TARGETS="
keycloak/keycloak-bootstrap-admin.sops.yaml
keycloak/keycloak-postgresql-credentials.sops.yaml
keycloak/keycloak-postgresql-tls.sops.yaml
keycloak/keycloak-tls.sops.yaml
vault/vault-tls.sops.yaml
redis/redis-tls.sops.yaml
redis/redis-auth.sops.yaml
redis/redis-backend-acl.sops.yaml
rabbitmq/rabbitmq-tls.sops.yaml
rabbitmq/rabbitmq-erlang-cookie.sops.yaml
rabbitmq/rabbitmq-bootstrap-auth.sops.yaml
"
for rel in $TARGETS; do
  if [ -e "$OUT/$rel" ] && [ "$FORCE" -ne 1 ]; then
    echo "ERROR: $OUT/$rel exists; pass --force to rotate" >&2
    exit 1
  fi
done

WORK=$(mktemp -d "${TMPDIR:-/tmp}/truehear-env-secrets.XXXXXX")
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
mkdir -p "$WORK/keycloak" "$WORK/vault" "$WORK/redis" "$WORK/rabbitmq"

PG_HOST=keycloak-postgresql.keycloak.svc.cluster.local
KC_HOST=keycloak.keycloak.svc.cluster.local
VAULT_INTERNAL=vault-internal.vault.svc.cluster.local
REDIS_HEADLESS=redis-headless.redis.svc.cluster.local
RMQ_HEADLESS=rabbitmq-headless.rabbitmq.svc.cluster.local
DAYS=825

rand() { openssl rand -base64 33 | tr -d '/+=' | cut -c1-32; }

# A-Za-z0-9_- only: characters that would break the Redis config parser
# (quotes, commas, slashes, equals) must not appear in redis-auth.conf.
rand_redis() {
  local out=""
  while [ ${#out} -lt 32 ]; do
    out="${out}$(openssl rand -base64 33 | tr -dc 'A-Za-z0-9_-')"
  done
  printf '%s' "$out" | cut -c1-32
}
sha256_hex() { printf '%s' "$1" | openssl dgst -sha256 | awk '{print $NF}'; }

indent() { sed 's/^/    /' "$1"; }

# ── PKI: one throwaway CA, one server leaf per TLS component ───────────────
CA_CRT="$WORK/ca.crt"
CA_KEY="$WORK/ca.key"
if [ -z "$KC_TLS" ] || [ -z "$PG_TLS" ] || [ -z "$VAULT_TLS" ] || [ -z "$REDIS_TLS" ] || [ -z "$RMQ_TLS" ]; then
  openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days "$DAYS" \
    -subj "/CN=truehear-platform-ca" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout "$CA_KEY" -out "$CA_CRT" 2>/dev/null
fi

leaf() { # leaf <basename> <cn> <extra-san-csv>
  openssl req -newkey rsa:2048 -nodes -sha256 -subj "/CN=$2" \
    -keyout "$WORK/$1.key" -out "$WORK/$1.csr" 2>/dev/null
  printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\nkeyUsage=digitalSignature,keyEncipherment\n' "$3" > "$WORK/$1.ext"
  openssl x509 -req -sha256 -days "$DAYS" -in "$WORK/$1.csr" \
    -CA "$CA_CRT" -CAkey "$CA_KEY" -CAcreateserial \
    -extfile "$WORK/$1.ext" -out "$WORK/$1.crt" 2>/dev/null
}
if [ -z "$PG_TLS" ]; then
  leaf pg "$PG_HOST" "DNS:$PG_HOST,DNS:keycloak-postgresql.keycloak.svc,DNS:keycloak-postgresql,DNS:keycloak-postgresql-internal.keycloak.svc.cluster.local"
fi
if [ -z "$KC_TLS" ]; then
  leaf kc "$KC_HOST" "DNS:$KC_HOST,DNS:keycloak.keycloak.svc,DNS:keycloak,DNS:localhost,IP:127.0.0.1"
fi
if [ -z "$VAULT_TLS" ]; then
  leaf vault "$VAULT_INTERNAL" "DNS:vault.vault.svc.cluster.local,DNS:$VAULT_INTERNAL,DNS:vault-0.$VAULT_INTERNAL,DNS:vault-1.$VAULT_INTERNAL,DNS:vault-2.$VAULT_INTERNAL,DNS:*.vault-internal.vault.svc.cluster.local,DNS:localhost,IP:127.0.0.1"
fi
if [ -z "$REDIS_TLS" ]; then
  leaf redis "$REDIS_HEADLESS" "DNS:redis.redis.svc.cluster.local,DNS:$REDIS_HEADLESS,DNS:redis-0.$REDIS_HEADLESS,DNS:redis-1.$REDIS_HEADLESS,DNS:redis-2.$REDIS_HEADLESS,DNS:redis-3.$REDIS_HEADLESS,DNS:redis-4.$REDIS_HEADLESS,DNS:redis-5.$REDIS_HEADLESS,DNS:*.redis-headless.redis.svc.cluster.local"
fi
if [ -z "$RMQ_TLS" ]; then
  leaf rmq "$RMQ_HEADLESS" "DNS:rabbitmq.rabbitmq.svc.cluster.local,DNS:$RMQ_HEADLESS,DNS:rabbitmq-0.$RMQ_HEADLESS,DNS:rabbitmq-1.$RMQ_HEADLESS,DNS:rabbitmq-2.$RMQ_HEADLESS,DNS:*.rabbitmq-headless.rabbitmq.svc.cluster.local,DNS:localhost"
fi

# Resolve the three PEMs each TLS Secret carries: operator files win.
resolve_pem() { # resolve_pem <name.crt|key|ca.crt> <operator-dir>
  local f="$1" dir="$2"
  if [ -n "$dir" ]; then
    printf '%s' "$dir/$f"
  else
    printf '%s' "$WORK/$1"
  fi
}

# ── Manifests (plaintext only inside $WORK) ─────────────────────────────────
PG_PASS=$(rand)
ADMIN_PASS=$(rand)
RMQ_PASS=$(rand)
REDIS_PASS=$(rand_redis)
BACKEND_ACL_PASS=$(rand)
COOKIE=$(openssl rand -hex 32)
ACL_USER="truehear-backend-$ENV"
ACL_SHA=$(sha256_hex "$BACKEND_ACL_PASS")
ACL_CMDS='~* +auth +hello +ping +client|setinfo +client|setname +cluster|slots +cluster|shards +asking +get +set +setex +del +expire +pexpire +incr +scan +decr +pttl +script|load +evalsha'

meta() { # meta <name> <namespace> <name-label> <component-label>
  cat <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: $1
  namespace: $2
  labels:
    app.kubernetes.io/name: $3
    app.kubernetes.io/component: $4
    app.kubernetes.io/part-of: truehear-platform
    truehear.io/environment: $ENV
EOF
}

meta keycloak-bootstrap-admin keycloak keycloak identity-provider > "$WORK/keycloak/keycloak-bootstrap-admin.sops.yaml"
cat >> "$WORK/keycloak/keycloak-bootstrap-admin.sops.yaml" <<EOF
type: Opaque
stringData:
  username: admin
  password: $ADMIN_PASS
EOF

meta keycloak-postgresql-credentials keycloak keycloak-postgresql database > "$WORK/keycloak/keycloak-postgresql-credentials.sops.yaml"
cat >> "$WORK/keycloak/keycloak-postgresql-credentials.sops.yaml" <<EOF
type: Opaque
stringData:
  database: keycloak
  username: keycloak
  password: $PG_PASS
EOF

{
  meta keycloak-postgresql-tls keycloak keycloak-postgresql database
  echo "type: kubernetes.io/tls"
  echo "stringData:"
  echo "  tls.crt: |"
  indent "$(resolve_pem pg.crt "$PG_TLS")"
  echo "  tls.key: |"
  indent "$(resolve_pem pg.key "$PG_TLS")"
  echo "  ca.crt: |"
  indent "$(resolve_pem ca.crt "$PG_TLS")"
} > "$WORK/keycloak/keycloak-postgresql-tls.sops.yaml"

{
  meta keycloak-tls keycloak keycloak identity-provider
  echo "type: kubernetes.io/tls"
  echo "stringData:"
  echo "  tls.crt: |"
  indent "$(resolve_pem kc.crt "$KC_TLS")"
  echo "  tls.key: |"
  indent "$(resolve_pem kc.key "$KC_TLS")"
  echo "  ca.crt: |"
  indent "$(resolve_pem ca.crt "$KC_TLS")"
} > "$WORK/keycloak/keycloak-tls.sops.yaml"

{
  meta vault-tls vault vault datastore
  echo "type: kubernetes.io/tls"
  echo "stringData:"
  echo "  tls.crt: |"
  indent "$(resolve_pem vault.crt "$VAULT_TLS")"
  echo "  tls.key: |"
  indent "$(resolve_pem vault.key "$VAULT_TLS")"
  echo "  ca.crt: |"
  indent "$(resolve_pem ca.crt "$VAULT_TLS")"
} > "$WORK/vault/vault-tls.sops.yaml"

{
  meta redis-tls redis redis cache
  echo "type: kubernetes.io/tls"
  echo "stringData:"
  echo "  tls.crt: |"
  indent "$(resolve_pem redis.crt "$REDIS_TLS")"
  echo "  tls.key: |"
  indent "$(resolve_pem redis.key "$REDIS_TLS")"
  echo "  ca.crt: |"
  indent "$(resolve_pem ca.crt "$REDIS_TLS")"
} > "$WORK/redis/redis-tls.sops.yaml"

{
  meta redis-auth redis redis cache
  echo "type: Opaque"
  echo "stringData:"
  echo "  redis-auth.conf: |"
  echo "    requirepass $REDIS_PASS"
  echo "    masterauth $REDIS_PASS"
} > "$WORK/redis/redis-auth.sops.yaml"

{
  meta redis-backend-acl redis redis cache
  echo "type: Opaque"
  echo "stringData:"
  echo "  users.conf: |"
  echo "    user $ACL_USER on #$ACL_SHA $ACL_CMDS"
  # Documented deviation: upstream keeps the ACL user's password out of band;
  # here the backend reads it from this Secret.
  echo "  password: $BACKEND_ACL_PASS"
} > "$WORK/redis/redis-backend-acl.sops.yaml"

{
  meta rabbitmq-tls rabbitmq rabbitmq message-broker
  echo "type: kubernetes.io/tls"
  echo "stringData:"
  echo "  tls.crt: |"
  indent "$(resolve_pem rmq.crt "$RMQ_TLS")"
  echo "  tls.key: |"
  indent "$(resolve_pem rmq.key "$RMQ_TLS")"
  echo "  ca.crt: |"
  indent "$(resolve_pem ca.crt "$RMQ_TLS")"
} > "$WORK/rabbitmq/rabbitmq-tls.sops.yaml"

meta rabbitmq-erlang-cookie rabbitmq rabbitmq message-broker > "$WORK/rabbitmq/rabbitmq-erlang-cookie.sops.yaml"
cat >> "$WORK/rabbitmq/rabbitmq-erlang-cookie.sops.yaml" <<EOF
type: Opaque
stringData:
  erlang-cookie: $COOKIE
EOF

meta rabbitmq-bootstrap-auth rabbitmq rabbitmq message-broker > "$WORK/rabbitmq/rabbitmq-bootstrap-auth.sops.yaml"
cat >> "$WORK/rabbitmq/rabbitmq-bootstrap-auth.sops.yaml" <<EOF
type: Opaque
stringData:
  username: truehear-admin
  password: $RMQ_PASS
EOF

# ── Encrypt in a staged path that matches .sops.yaml, then move ─────────────
# sops resolves creation rules against the path relative to the .sops.yaml it
# finds; stage under a relative path that contains workload/ so the repo rule
# matches, and run sops from the work dir with --config pointing at the repo.
STAGE_REL="stage/workload/environments/$ENV"
STAGE="$WORK/$STAGE_REL"
mkdir -p "$STAGE"
for rel in $TARGETS; do
  mkdir -p "$STAGE/$(dirname "$rel")"
  cp "$WORK/$rel" "$STAGE/$rel"
  (
    cd "$WORK"
    sops --config "$REPO_ROOT/.sops.yaml" --encrypt --in-place --input-type yaml --output-type yaml "$STAGE_REL/$rel"
  )
  grep -q 'ENC\[AES256_GCM' "$STAGE/$rel" || { echo "ERROR: $rel did not encrypt" >&2; exit 1; }
  mv "$STAGE/$rel" "$OUT/$rel"
  echo "wrote $OUT/$rel"
done

echo
echo "Environment: $ENV (CA: one throwaway per run, $DAYS days)"
echo "Read any admin password with: mise run sops-decrypt <file>, e.g."
echo "  mise run sops-decrypt $OUT/keycloak/keycloak-bootstrap-admin.sops.yaml"
echo "  mise run sops-decrypt $OUT/rabbitmq/rabbitmq-bootstrap-auth.sops.yaml"
echo "The Redis backend ACL user is $ACL_USER; its plaintext password is the"
echo "'password' key of $OUT/redis/redis-backend-acl.sops.yaml."
