#!/bin/sh
# Second init step of a production database, after init.sql: one random password per role, and the audit
# log owned by a role the audit service never logs in as, so it cannot disable the append-only triggers.
set -eu
for schema in identity settings mail privacy orchestrator approval jira ops audit reporting; do
  password="$(cat "/run/secrets/postgres/${schema}_password")"
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -c "ALTER ROLE ${schema}_svc PASSWORD '$password'"
done
owner="$(cat /run/secrets/postgres/audit_owner_password)"
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
CREATE ROLE audit_owner LOGIN PASSWORD '$owner';
ALTER ROLE audit_owner SET search_path = audit, public;
GRANT USAGE, CREATE ON SCHEMA audit TO audit_owner;
REVOKE CREATE ON SCHEMA audit FROM audit_svc;
ALTER DEFAULT PRIVILEGES FOR ROLE audit_owner IN SCHEMA audit GRANT SELECT, INSERT ON TABLES TO audit_svc;
ALTER DEFAULT PRIVILEGES FOR ROLE audit_owner IN SCHEMA audit GRANT USAGE, SELECT ON SEQUENCES TO audit_svc;
SQL
