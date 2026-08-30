#!/bin/sh
set -eu

: "${POSTGRES_HOST:?POSTGRES_HOST is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${LEDGERLITE_MIGRATOR_PASSWORD:?LEDGERLITE_MIGRATOR_PASSWORD is required}"
: "${LEDGERLITE_APP_PASSWORD:?LEDGERLITE_APP_PASSWORD is required}"

psql -X \
    --host="${POSTGRES_HOST}" \
    --username="${POSTGRES_USER}" \
    --dbname="${POSTGRES_DB}" \
    --set=ON_ERROR_STOP=1 \
    --set=database_name="${POSTGRES_DB}" \
    --set=migrator_password="${LEDGERLITE_MIGRATOR_PASSWORD}" \
    --set=runtime_password="${LEDGERLITE_APP_PASSWORD}" <<'SQL'
SELECT format(
    'CREATE ROLE ledgerlite_migrator LOGIN PASSWORD %L',
    :'migrator_password'
)
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ledgerlite_migrator')
\gexec

SELECT format(
    'CREATE ROLE ledgerlite_app LOGIN PASSWORD %L',
    :'runtime_password'
)
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ledgerlite_app')
\gexec

ALTER ROLE ledgerlite_migrator WITH
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION
    PASSWORD :'migrator_password';
ALTER ROLE ledgerlite_app WITH
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION
    PASSWORD :'runtime_password';

-- Preserve data from the original single-role Compose setup while handing
-- ownership of existing ledger objects to the dedicated migration identity.
-- The bootstrap role also owns objects required by PostgreSQL itself, so a
-- database-wide REASSIGN OWNED is not valid for that role.
SELECT format(
    'ALTER %s %I.%I OWNER TO ledgerlite_migrator',
    CASE relkind WHEN 'S' THEN 'SEQUENCE' ELSE 'TABLE' END,
    nspname,
    relname
)
FROM pg_class
JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
WHERE nspname = 'public'
  AND relkind IN ('r', 'p', 'S')
  AND pg_get_userbyid(relowner) = 'ledgerlite'
ORDER BY relkind = 'S', relname
\gexec

SELECT format(
    'ALTER FUNCTION %I.%I(%s) OWNER TO ledgerlite_migrator',
    nspname,
    proname,
    pg_get_function_identity_arguments(pg_proc.oid)
)
FROM pg_proc
JOIN pg_namespace ON pg_namespace.oid = pg_proc.pronamespace
WHERE nspname = 'public'
  AND prokind = 'f'
  AND pg_get_userbyid(proowner) = 'ledgerlite'
ORDER BY proname, pg_get_function_identity_arguments(pg_proc.oid)
\gexec

SELECT format(
    'REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC',
    :'database_name'
)
\gexec
SELECT format(
    'GRANT CONNECT ON DATABASE %I TO ledgerlite_migrator, ledgerlite_app',
    :'database_name'
)
\gexec
SELECT format(
    'REVOKE CREATE, TEMPORARY ON DATABASE %I FROM ledgerlite_app',
    :'database_name'
)
\gexec

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO ledgerlite_migrator;
GRANT USAGE ON SCHEMA public TO ledgerlite_app;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ledgerlite_app;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM ledgerlite_app;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM ledgerlite_app;

-- Trigger functions execute through their triggers. Only the deferred balance
-- assertion and the derived-evidence helper need direct runtime EXECUTE.
SELECT 'GRANT EXECUTE ON FUNCTION public.ledger_assert_balanced(uuid) TO ledgerlite_app'
WHERE to_regprocedure('public.ledger_assert_balanced(uuid)') IS NOT NULL
\gexec
SELECT 'GRANT EXECUTE ON FUNCTION public.reconciliation_insert_ledger_only(uuid, uuid, uuid) TO ledgerlite_app'
WHERE to_regprocedure(
    'public.reconciliation_insert_ledger_only(uuid, uuid, uuid)'
) IS NOT NULL
\gexec

-- These relations appear only after migrations. The second, post-migration run
-- grants the runtime identity access only to application relations; it cannot
-- read or mutate Alembic's version table.
SELECT format('GRANT SELECT ON TABLE public.%I TO ledgerlite_app', relname)
FROM pg_class
WHERE relnamespace = 'public'::regnamespace
  AND relkind IN ('r', 'p')
  AND relname IN (
      'accounts',
      'ledger_transactions',
      'ledger_entries',
      'outbox_events',
      'reconciliation_runs',
      'reconciliation_items'
  )
\gexec

SELECT format('GRANT INSERT ON TABLE public.%I TO ledgerlite_app', relname)
FROM pg_class
WHERE relnamespace = 'public'::regnamespace
  AND relkind IN ('r', 'p')
  AND relname IN (
      'accounts',
      'ledger_entries'
  )
\gexec

SELECT 'GRANT INSERT (
    id,
    type,
    amount,
    currency,
    source_account_id,
    destination_account_id,
    idempotency_key,
    request_fingerprint,
    response_payload,
    reverses_transaction_id,
    reversal_reason_code,
    reversal_note,
    created_at
) ON TABLE public.ledger_transactions TO ledgerlite_app'
WHERE to_regclass('public.ledger_transactions') IS NOT NULL
\gexec

SELECT 'GRANT INSERT (
    event_type,
    aggregate_type,
    aggregate_id,
    request_id,
    payload,
    created_at
) ON TABLE public.outbox_events TO ledgerlite_app'
WHERE to_regclass('public.outbox_events') IS NOT NULL
\gexec

-- PostgreSQL requires an UPDATE privilege to acquire SELECT ... FOR UPDATE
-- row locks. These two identity columns are lock tickets only: account currency
-- is protected by the account-identity trigger and every ledger-transaction
-- UPDATE is rejected by the immutable-row trigger.
SELECT 'GRANT UPDATE (currency) ON TABLE public.accounts TO ledgerlite_app'
WHERE to_regclass('public.accounts') IS NOT NULL
\gexec
SELECT 'GRANT UPDATE (id) ON TABLE public.ledger_transactions TO ledgerlite_app'
WHERE to_regclass('public.ledger_transactions') IS NOT NULL
\gexec

-- Mutation is limited to the operational reconciliation workflow. Journal and
-- outbox rows remain immutable; the two column-scoped UPDATE grants above are
-- lock tickets whose protected values cannot actually be changed.
SELECT 'GRANT UPDATE (status, summary, completed_at) ON TABLE public.reconciliation_runs TO ledgerlite_app'
WHERE to_regclass('public.reconciliation_runs') IS NOT NULL
\gexec
SELECT 'GRANT UPDATE (result, mismatch_code, matched_transaction_id, resolution_status, resolution_note, resolved_at) ON TABLE public.reconciliation_items TO ledgerlite_app'
WHERE to_regclass('public.reconciliation_items') IS NOT NULL
\gexec

ALTER DEFAULT PRIVILEGES FOR ROLE ledgerlite_migrator IN SCHEMA public
    REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE ledgerlite_migrator IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE ledgerlite_migrator IN SCHEMA public
    REVOKE ALL ON FUNCTIONS FROM PUBLIC;

ALTER ROLE ledgerlite_app SET statement_timeout = '10s';
ALTER ROLE ledgerlite_app SET lock_timeout = '3s';

-- The official image's local socket is private to bootstrap services. Remove
-- password authentication from the legacy superuser so it cannot be reached
-- from the application network, including on upgraded volumes.
ALTER ROLE ledgerlite PASSWORD NULL;
SQL

if [ "${LEDGERLITE_FINALIZE_GRANTS:-0}" = "1" ]; then
    psql -X \
        --host="${POSTGRES_HOST}" \
        --username="${POSTGRES_USER}" \
        --dbname="${POSTGRES_DB}" \
        --set=ON_ERROR_STOP=1 \
        --command="ALTER ROLE ledgerlite_migrator NOLOGIN"
fi
