-- One schema per service. A service connects with its own role whose search_path is its schema,
-- and has no privileges on any other schema (database-per-service, enforced by Postgres).
CREATE EXTENSION IF NOT EXISTS vector;

DO $$
DECLARE
  svc text;
BEGIN
  FOREACH svc IN ARRAY ARRAY[
    'identity', 'settings', 'mail', 'privacy', 'orchestrator',
    'approval', 'jira', 'ops', 'audit', 'reporting'
  ] LOOP
    EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', svc);
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = svc || '_svc') THEN
      EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L', svc || '_svc', svc || '_dev_password');
    END IF;
    EXECUTE format('GRANT USAGE, CREATE ON SCHEMA %I TO %I', svc, svc || '_svc');
    EXECUTE format('ALTER ROLE %I SET search_path = %I, public', svc || '_svc', svc);
    EXECUTE format('REVOKE ALL ON SCHEMA %I FROM PUBLIC', svc);
  END LOOP;
END $$;
