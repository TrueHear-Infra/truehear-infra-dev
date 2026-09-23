# Local Vault and RabbitMQ administrator runbook

This runbook explains how to create local human administrator accounts for
Vault and RabbitMQ, expose their TLS-protected management interfaces through
`kubectl port-forward`, and verify both accounts safely.

These instructions are for the Docker-based `local-host` environment. They do
not replace Keycloak, OIDC, or another centralized identity provider in a cloud
environment.

## Table of contents

- [Goal](#goal)
- [What is already enabled](#what-is-already-enabled)
- [Security rules](#security-rules)
- [Step 1: Verify the workload services](#step-1-verify-the-workload-services)
- [Step 2: Prepare local DNS](#step-2-prepare-local-dns)
- [Step 3: Open the Vault port-forward](#step-3-open-the-vault-port-forward)
- [Step 4: Create the Vault administrator policy](#step-4-create-the-vault-administrator-policy)
- [Step 5: Enable Vault username authentication](#step-5-enable-vault-username-authentication)
- [Step 6: Create the Vault administrator](#step-6-create-the-vault-administrator)
- [Step 7: Test the Vault account](#step-7-test-the-vault-account)
- [Step 8: Open the RabbitMQ port-forward](#step-8-open-the-rabbitmq-port-forward)
- [Step 9: Create the RabbitMQ administrator](#step-9-create-the-rabbitmq-administrator)
- [Step 10: Test the RabbitMQ account](#step-10-test-the-rabbitmq-account)
- [Credential rotation](#credential-rotation)
- [Restart and teardown behavior](#restart-and-teardown-behavior)
- [Troubleshooting](#troubleshooting)
- [Completion checklist](#completion-checklist)

## Goal

The final access paths are:

```text
Browser or local command
        |
        | HTTPS through kubectl port-forward
        v
Vault UI :8200
        |
        +--> userpass login
        +--> local-admin policy

Browser or local command
        |
        | HTTPS through kubectl port-forward
        v
RabbitMQ management UI :15671
        |
        +--> internal RabbitMQ login
        +--> administrator tag
        +--> full permissions on the truehear vhost
```

The Vault and RabbitMQ accounts are separate identities. Giving a user access
to one service does not grant access to the other.

## What is already enabled

No Helm change is required to expose these interfaces locally.

Vault already has:

```yaml
server:
  ha:
    raft:
      config: |
        ui = true

ui:
  enabled: true
  serviceType: ClusterIP
  externalPort: 8200
```

RabbitMQ already has:

- The `rabbitmq_management` plugin.
- An HTTPS-only management listener on port `15671`.
- A ClusterIP service exposing port `15671`.
- Internal username and password authentication while OIDC is disabled.

The interfaces remain private inside Kubernetes until a local port-forward is
opened.

## Security rules

- Never commit the Vault root token or either administrator password.
- Store administrator passwords in a password manager.
- Use the Vault root token only to configure the human login, then unset it.
- Do not reuse the RabbitMQ backend account for human administration.
- Do not reuse either local administrator account in a cloud environment.
- Do not put these passwords into Kubernetes manifests or `.env` files.
- Do not use `curl -k` or disable browser certificate validation.
- Keep port-forward terminals open only while the interfaces are needed.

## Step 1: Verify the workload services

Run these commands from the `truehear-infra-dev` repository root.

Verify Vault:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods,service --namespace vault
```

All three Vault Pods should be `1/1 Running`. If they are sealed after a
restart, unseal them before continuing.

Verify RabbitMQ:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get pods,service --namespace rabbitmq
```

All three RabbitMQ Pods should be `1/1 Running`.

Confirm the expected service ports:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get service vault-ui \
  --namespace vault \
  --output='jsonpath={.spec.ports[*].port}{"\n"}'

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get service rabbitmq \
  --namespace rabbitmq \
  --output='jsonpath={.spec.ports[*].port}{"\n"}'
```

Expected management ports:

```text
Vault:    8200
RabbitMQ: 15671
```

## Step 2: Prepare local DNS

The TLS certificates use Kubernetes DNS names. Add this entry to `/etc/hosts`
if it does not already exist:

```text
127.0.0.1 vault.vault.svc.cluster.local rabbitmq.rabbitmq.svc.cluster.local
```

Verify resolution:

```bash
dscacheutil -q host -a name vault.vault.svc.cluster.local
dscacheutil -q host -a name rabbitmq.rabbitmq.svc.cluster.local
```

Both names should resolve to `127.0.0.1`.

These services use the private TrueHear CA. Import the private root CA into the
macOS login keychain through Keychain Access if the browser does not already
trust it. Import only the public root certificate, never a CA private key.

## Step 3: Open the Vault port-forward

Open the Vault tunnel in its own terminal:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl port-forward \
  --namespace vault \
  pod/vault-0 \
  8200:8200
```

Expected output:

```text
Forwarding from 127.0.0.1:8200 -> 8200
Forwarding from [::1]:8200 -> 8200
```

Keep this terminal running. The tunnel is available only while this command is
running.

The direct Pod tunnel and this service tunnel are equivalent for this local
test:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl port-forward \
  --namespace vault \
  service/vault-ui \
  8200:8200
```

Use only one of them because both require local port `8200`.

## Step 4: Create the Vault administrator policy

Open a second terminal and read the Vault root token without displaying it:

```zsh
read -s "vault_root_token?Vault root token: "
echo
test -n "$vault_root_token"
```

Create the local administrator policy:

```bash
printf '%s\n' \
  'path "*" {' \
  '  capabilities = ["create", "read", "update", "patch", "delete", "list", "sudo"]' \
  '}' |
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec -i --namespace vault vault-0 -- \
  env \
    VAULT_ADDR=https://127.0.0.1:8200 \
    VAULT_SKIP_VERIFY=true \
    VAULT_TOKEN="$vault_root_token" \
    vault policy write local-admin -
```

`VAULT_SKIP_VERIFY` is used only inside the Vault Pod while connecting to its
own loopback address. Browser and host-side verification still use the CA.

Verify the policy exists without printing the policy body:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace vault vault-0 -- \
  env \
    VAULT_ADDR=https://127.0.0.1:8200 \
    VAULT_SKIP_VERIFY=true \
    VAULT_TOKEN="$vault_root_token" \
    vault policy list
```

The output must include `local-admin`.

## Step 5: Enable Vault username authentication

Check the enabled authentication methods:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace vault vault-0 -- \
  env \
    VAULT_ADDR=https://127.0.0.1:8200 \
    VAULT_SKIP_VERIFY=true \
    VAULT_TOKEN="$vault_root_token" \
    vault auth list
```

If `userpass/` is missing, enable it:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace vault vault-0 -- \
  env \
    VAULT_ADDR=https://127.0.0.1:8200 \
    VAULT_SKIP_VERIFY=true \
    VAULT_TOKEN="$vault_root_token" \
    vault auth enable userpass
```

If Vault reports that the path is already in use, `userpass` is already
enabled and this step does not need to be repeated.

## Step 6: Create the Vault administrator

Choose a username and enter a password without displaying it:

```zsh
read "vault_admin_username?Vault admin username [local-admin]: "
vault_admin_username=${vault_admin_username:-local-admin}

read -s "vault_admin_password?Vault admin password: "
echo
test -n "$vault_admin_password"
```

Create or update the user. `password=-` makes Vault read the password from
standard input instead of exposing it in shell history.

```bash
printf '%s' "$vault_admin_password" |
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec -i --namespace vault vault-0 -- \
  env \
    VAULT_ADDR=https://127.0.0.1:8200 \
    VAULT_SKIP_VERIFY=true \
    VAULT_TOKEN="$vault_root_token" \
    vault write \
      "auth/userpass/users/${vault_admin_username}" \
      password=- \
      policies=local-admin
```

Remove the root token and password from the shell immediately:

```bash
unset vault_root_token vault_admin_password
```

Keep only the username in the shell for the verification step.

## Step 7: Test the Vault account

### Test with the Vault UI

Open:

```text
https://vault.vault.svc.cluster.local:8200/ui
```

Use:

```text
Method:     Username
Mount path: userpass
Username:   local-admin, or the username selected in Step 6
Password:   value stored in the password manager
```

After login, the account should be able to view the enabled secrets engines,
authentication methods, and policies.

### Test without displaying a Vault token

The following command prompts interactively for the password and suppresses
the returned token:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec -it --namespace vault vault-0 -- \
  env \
    VAULT_ADDR=https://127.0.0.1:8200 \
    VAULT_SKIP_VERIFY=true \
    vault login \
      -no-print \
      -no-store \
      -method=userpass \
      "username=${vault_admin_username:-local-admin}"
```

Enter the administrator password when Vault prompts for it. A successful
command confirms the account can authenticate without saving or printing the
issued Vault token.

```bash
unset vault_admin_username
```

## Step 8: Open the RabbitMQ port-forward

Open a third terminal and keep it running:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl port-forward \
  --namespace rabbitmq \
  service/rabbitmq \
  15671:15671
```

Expected output:

```text
Forwarding from 127.0.0.1:15671 -> 15671
Forwarding from [::1]:15671 -> 15671
```

## Step 9: Create the RabbitMQ administrator

Inspect existing usernames and tags:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace rabbitmq rabbitmq-0 -- \
  rabbitmqctl list_users
```

Create a separate human administrator. Do not use the backend AMQP account.

```zsh
read "rabbitmq_admin_username?RabbitMQ admin username [local-admin]: "
rabbitmq_admin_username=${rabbitmq_admin_username:-local-admin}

read -s "rabbitmq_admin_password?RabbitMQ admin password: "
echo
test -n "$rabbitmq_admin_password"

printf '%s\n' "$rabbitmq_admin_password" |
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec -i --namespace rabbitmq rabbitmq-0 -- \
  rabbitmqctl add_user "$rabbitmq_admin_username"
```

The standard-input form keeps the password out of shell history.

Give the user full management UI capabilities:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace rabbitmq rabbitmq-0 -- \
  rabbitmqctl set_user_tags \
    "$rabbitmq_admin_username" \
    administrator
```

Grant full configure, write, and read access to the `truehear` virtual host:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace rabbitmq rabbitmq-0 -- \
  rabbitmqctl set_permissions \
    --vhost truehear \
    "$rabbitmq_admin_username" \
    '.*' '.*' '.*'
```

Verify the tag and permissions:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace rabbitmq rabbitmq-0 -- \
  rabbitmqctl list_users

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace rabbitmq rabbitmq-0 -- \
  rabbitmqctl list_user_permissions "$rabbitmq_admin_username"
```

Expected values include:

```text
[administrator]
truehear    .*    .*    .*
```

Remove the password from the shell:

```bash
unset rabbitmq_admin_password
```

## Step 10: Test the RabbitMQ account

### Test with the RabbitMQ UI

Open:

```text
https://rabbitmq.rabbitmq.svc.cluster.local:15671
```

Log in with the RabbitMQ administrator username and password from Step 9.

Verify that the UI shows:

- All three cluster nodes.
- The `truehear` virtual host.
- Connections, channels, exchanges, and queues.
- The administration section.

### Test through the HTTPS API

Export only the public RabbitMQ CA certificate to a temporary file:

```bash
rabbitmq_ca_file=$(mktemp)
chmod 600 "$rabbitmq_ca_file"

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl get secret rabbitmq-tls \
  --namespace rabbitmq \
  --output='jsonpath={.data.ca\.crt}' |
  base64 --decode >"$rabbitmq_ca_file"
```

Call the identity endpoint. `curl` prompts for the password because only the
username is provided.

```bash
curl \
  --fail-with-body \
  --silent \
  --show-error \
  --cacert "$rabbitmq_ca_file" \
  --resolve rabbitmq.rabbitmq.svc.cluster.local:15671:127.0.0.1 \
  --user "${rabbitmq_admin_username:-local-admin}" \
  https://rabbitmq.rabbitmq.svc.cluster.local:15671/api/whoami |
  jq '{name, tags}'
```

Expected result:

```json
{
  "name": "local-admin",
  "tags": ["administrator"]
}
```

Remove the temporary public certificate and username variable:

```bash
rm "$rabbitmq_ca_file"
unset rabbitmq_ca_file rabbitmq_admin_username
```

## Credential rotation

### Rotate the Vault administrator password

Repeat Step 6 with the same username. Writing the same user path updates its
password and policy assignment.

### Rotate the RabbitMQ administrator password

```zsh
read "rabbitmq_admin_username?RabbitMQ admin username [local-admin]: "
rabbitmq_admin_username=${rabbitmq_admin_username:-local-admin}

read -s "rabbitmq_admin_password?New RabbitMQ admin password: "
echo
test -n "$rabbitmq_admin_password"

printf '%s\n' "$rabbitmq_admin_password" |
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec -i --namespace rabbitmq rabbitmq-0 -- \
  rabbitmqctl change_password "$rabbitmq_admin_username"

unset rabbitmq_admin_username rabbitmq_admin_password
```

Update the password manager immediately after either rotation.

## Restart and teardown behavior

| Event                     | Vault administrator | RabbitMQ administrator |
| ------------------------- | ------------------- | ---------------------- |
| Pod restart               | Preserved           | Preserved              |
| StatefulSet rollout       | Preserved           | Preserved              |
| Workload node restart     | Preserved on PVC    | Preserved on PVC       |
| Full local-host teardown  | Recreate account    | Recreate account       |
| PVC deletion or data loss | Recreate account    | Recreate account       |

Vault stores its account and policy in Raft storage. RabbitMQ stores its user,
tags, and permissions in its replicated database. Kubernetes Secrets are not
used for these two human accounts.

## Troubleshooting

### Local port is already in use

Find the process holding the port:

```bash
lsof -nP -iTCP:8200 -sTCP:LISTEN
lsof -nP -iTCP:15671 -sTCP:LISTEN
```

If an existing correct port-forward is running, reuse it. Otherwise, stop only
the stale process identified by `lsof`.

### Vault UI loads but username login is unavailable

Verify that `userpass/` is enabled:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace vault vault-0 -- \
  env \
    VAULT_ADDR=https://127.0.0.1:8200 \
    VAULT_SKIP_VERIFY=true \
    vault auth list
```

An unauthenticated request may return permission denied. If so, repeat the
authenticated command from Step 5 with the root token.

### Vault reports that it is sealed

Unseal all Vault members using the protected unseal shares. Do not initialize
Vault again. Follow the recovery section in
[the Vault runbook](vault-local-host.md#restart-and-recovery-behavior).

### RabbitMQ says the user already exists

Do not delete the account. Either use the existing password or rotate it with
the `change_password` command in the credential rotation section.

### RabbitMQ login succeeds but resources are missing

Check both the administrator tag and the `truehear` vhost permissions:

```bash
KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace rabbitmq rabbitmq-0 -- \
  rabbitmqctl list_users

KUBECONFIG="$PWD/local-workload.kubeconfig" \
kubectl exec --namespace rabbitmq rabbitmq-0 -- \
  rabbitmqctl list_user_permissions local-admin
```

### The browser reports an untrusted certificate

Confirm that the browser URL uses the Kubernetes DNS name, not `localhost` or
`127.0.0.1`. Then confirm that the public TrueHear root CA is trusted in the
macOS login keychain. Never import a CA private key.

## Completion checklist

- [ ] Vault, RabbitMQ, and their services are healthy.
- [ ] Both Kubernetes DNS names resolve to `127.0.0.1` locally.
- [ ] The Vault port-forward is listening on port `8200`.
- [ ] Vault `userpass/` authentication is enabled.
- [ ] The Vault administrator has the `local-admin` policy.
- [ ] Vault UI and CLI authentication succeed without exposing a token.
- [ ] The RabbitMQ port-forward is listening on port `15671`.
- [ ] The RabbitMQ administrator has the `administrator` tag.
- [ ] The RabbitMQ administrator has full permissions on `truehear`.
- [ ] RabbitMQ UI and `/api/whoami` authentication succeed.
- [ ] Passwords are stored only in the password manager.
- [ ] Root tokens and passwords are unset from shell variables.

## References

- [Vault userpass authentication](https://developer.hashicorp.com/vault/docs/auth/userpass)
- [Vault login command](https://developer.hashicorp.com/vault/docs/commands/login)
- [Vault write command](https://developer.hashicorp.com/vault/docs/commands/write)
- [RabbitMQ management plugin](https://www.rabbitmq.com/docs/management)
- [RabbitMQ access control](https://www.rabbitmq.com/docs/access-control)
