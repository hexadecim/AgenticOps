#!/bin/sh
# Writes deploy/prod/secrets/: one folder per service, holding only what that service may know. Files are named
# after settings fields (SECRETS_DIR). A secrets manager (Vault Agent, Kubernetes CSI, ...) can write the same
# layout instead; see docs/adr/0009-production-hardening.md. Re-running keeps what exists: nothing is rotated.
set -eu
cd "$(dirname "$0")"
OUT=secrets
SERVICES="identity-service api-gateway settings-service mail-service privacy-service audit-service approval-service jira-connector agent-orchestrator reporting-service ops-service"

mkdir -p "$OUT"
chmod 700 "$OUT"   # the host folder is closed; containers mount the service folders below it
random() { openssl rand -hex 32; }
put() { # file value: keeps an existing file
  [ -f "$1" ] || { printf '%s' "$2" > "$1"; chmod 644 "$1"; }
}
pair() { # private public
  if [ ! -f "$1" ]; then
    openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$1" 2>/dev/null
    openssl pkey -in "$1" -pubout -out "$2"
    chmod 644 "$1" "$2"
  fi
}
fernet() { openssl rand -base64 32 | tr '+/' '-_'; }
schema_of() {
  case "$1" in
    identity-service) echo identity ;; settings-service) echo settings ;; mail-service) echo mail ;;
    privacy-service) echo privacy ;; audit-service) echo audit ;; approval-service) echo approval ;;
    jira-connector) echo jira ;; agent-orchestrator) echo orchestrator ;; reporting-service) echo reporting ;;
    ops-service) echo ops ;; *) echo "" ;;
  esac
}

mkdir -p "$OUT/postgres" "$OUT/rabbitmq" "$OUT/keys/services" "$OUT/tls"
put "$OUT/postgres/superuser_password" "$(random)"
put "$OUT/postgres/audit_owner_password" "$(random)"
put "$OUT/rabbitmq/password" "$(random)"
RABBIT="$(cat "$OUT/rabbitmq/password")"
put "$OUT/rabbitmq/rabbitmq.conf" "default_user = opspilot
default_pass = $RABBIT
"
pair "$OUT/keys/session_private.pem" "$OUT/keys/session_public.pem"

for service in $SERVICES; do
  pair "$OUT/keys/$service.private.pem" "$OUT/keys/services/$service.pem"
done

for service in $SERVICES; do
  dir="$OUT/$service"
  mkdir -p "$dir/keys/services"
  put "$dir/rabbitmq_url" "amqp://opspilot:$RABBIT@rabbitmq:5672/"
  cp "$OUT/keys/session_public.pem" "$dir/keys/session_public.pem"
  cp "$OUT/keys/$service.private.pem" "$dir/keys/service_private.pem"
  cp "$OUT"/keys/services/*.pem "$dir/keys/services/"
  schema="$(schema_of "$service")"
  if [ -n "$schema" ]; then
    put "$OUT/postgres/${schema}_password" "$(random)"
    put "$dir/database_url" "postgresql+asyncpg://${schema}_svc:$(cat "$OUT/postgres/${schema}_password")@postgres:5432/opspilot"
  fi
done

# Only identity-service signs sessions and sign-in challenges.
cp "$OUT/keys/session_private.pem" "$OUT/identity-service/keys/session_private.pem"
put "$OUT/identity-service/jwt_secret" "$(random)"
# One encryption key per service that encrypts anything: losing one exposes only that service's data.
for service in identity-service settings-service privacy-service; do
  put "$OUT/$service/fernet_key" "$(fernet)"
done

# Migrations of the audit schema run as its owner; the service itself may only add and read (rule 8).
mkdir -p "$OUT/audit-migrate"
cp -R "$OUT/audit-service/keys" "$OUT/audit-migrate/"
put "$OUT/audit-migrate/database_url" "postgresql+asyncpg://audit_owner:$(cat "$OUT/postgres/audit_owner_password")@postgres:5432/opspilot"

# A self-signed certificate so the stack starts; replace both files with real ones.
if [ ! -f "$OUT/tls/privkey.pem" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=${TLS_HOSTNAME:-localhost}" \
    -addext "subjectAltName=DNS:${TLS_HOSTNAME:-localhost}" \
    -keyout "$OUT/tls/privkey.pem" -out "$OUT/tls/fullchain.pem" 2>/dev/null
  chmod 644 "$OUT/tls/privkey.pem"
fi
echo "Secrets are in deploy/prod/$OUT. Replace tls/ with a real certificate before going live."
