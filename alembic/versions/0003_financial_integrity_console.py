"""Add reversals, reconciliation, and the transactional event outbox.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts", sa.Column("display_name", sa.String(length=80), nullable=True)
    )
    op.create_check_constraint(
        "ck_accounts_display_name",
        "accounts",
        "display_name IS NULL OR "
        "(display_name = btrim(display_name) AND length(display_name) > 0)",
    )

    op.add_column(
        "ledger_transactions",
        sa.Column(
            "reverses_transaction_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "ledger_transactions",
        sa.Column("reversal_reason_code", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "ledger_transactions",
        sa.Column("reversal_note", sa.String(length=240), nullable=True),
    )
    op.create_foreign_key(
        "fk_ledger_tx_reverses_ledger_tx",
        "ledger_transactions",
        "ledger_transactions",
        ["reverses_transaction_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_ledger_transactions_reverses_transaction_id",
        "ledger_transactions",
        ["reverses_transaction_id"],
    )

    op.drop_constraint(
        "ck_ledger_transactions_type", "ledger_transactions", type_="check"
    )
    op.create_check_constraint(
        "ck_ledger_transactions_type",
        "ledger_transactions",
        "type IN ('deposit', 'transfer', 'reversal')",
    )
    op.drop_constraint(
        "ck_ledger_transactions_idempotency_pair",
        "ledger_transactions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_ledger_transactions_idempotency_pair",
        "ledger_transactions",
        """
        (
            type = 'deposit'
            AND
            (
                (idempotency_key IS NULL AND request_fingerprint IS NULL)
                OR
                (idempotency_key IS NOT NULL AND request_fingerprint IS NOT NULL)
            )
        )
        OR
        (
            type IN ('transfer', 'reversal')
            AND idempotency_key IS NOT NULL
            AND request_fingerprint IS NOT NULL
        )
        """,
    )
    op.create_check_constraint(
        "ck_ledger_transactions_reversal_fields",
        "ledger_transactions",
        """
        (
            type = 'reversal'
            AND reverses_transaction_id IS NOT NULL
            AND reversal_reason_code IS NOT NULL
        )
        OR
        (
            type <> 'reversal'
            AND reverses_transaction_id IS NULL
            AND reversal_reason_code IS NULL
            AND reversal_note IS NULL
        )
        """,
    )
    op.create_check_constraint(
        "ck_ledger_transactions_reversal_reason",
        "ledger_transactions",
        """
        reversal_reason_code IS NULL
        OR reversal_reason_code IN (
            'duplicate',
            'customer_request',
            'operator_correction',
            'other'
        )
        """,
    )
    op.create_check_constraint(
        "ck_ledger_transactions_reversal_note",
        "ledger_transactions",
        """
        reversal_note IS NULL
        OR (
            reversal_note = btrim(reversal_note)
            AND length(reversal_note) > 0
        )
        """,
    )
    op.create_check_constraint(
        "ck_ledger_transactions_not_self_reversal",
        "ledger_transactions",
        "reverses_transaction_id IS NULL OR reverses_transaction_id <> id",
    )

    # PostgreSQL sequences allocate before commit, so a plain identity column can
    # expose a later rollback/commit behind a keyset cursor. The trigger takes the
    # same transaction-order advisory lock used by the outbox before allocating
    # the durable posting sequence. Existing rows are backfilled deterministically.
    op.execute("CREATE SEQUENCE ledger_transactions_posting_sequence_seq")
    op.add_column(
        "ledger_transactions",
        sa.Column("posting_sequence", sa.BigInteger(), nullable=True),
    )
    op.execute(
        "ALTER TABLE ledger_transactions "
        "DISABLE TRIGGER ledger_transactions_are_immutable"
    )
    op.execute(
        """
        WITH ordered_transactions AS (
            SELECT
                id,
                row_number() OVER (ORDER BY created_at, id) AS posting_sequence
            FROM ledger_transactions
        )
        UPDATE ledger_transactions
        SET posting_sequence = ordered_transactions.posting_sequence
        FROM ordered_transactions
        WHERE ledger_transactions.id = ordered_transactions.id
        """
    )
    op.execute(
        "ALTER TABLE ledger_transactions "
        "ENABLE TRIGGER ledger_transactions_are_immutable"
    )
    op.execute(
        """
        SELECT setval(
            'ledger_transactions_posting_sequence_seq',
            COALESCE(max(posting_sequence), 0) + 1,
            false
        )
        FROM ledger_transactions
        """
    )
    op.alter_column("ledger_transactions", "posting_sequence", nullable=False)
    op.create_unique_constraint(
        "uq_ledger_transactions_posting_sequence",
        "ledger_transactions",
        ["posting_sequence"],
    )
    op.execute(
        "ALTER SEQUENCE ledger_transactions_posting_sequence_seq "
        "OWNED BY ledger_transactions.posting_sequence"
    )
    op.execute(
        """
        CREATE FUNCTION ledger_assign_posting_sequence()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock(5814211102026082026);
            IF NEW.posting_sequence IS NULL THEN
                NEW.posting_sequence := nextval(
                    'public.ledger_transactions_posting_sequence_seq'
                );
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION ledger_assign_posting_sequence() FROM PUBLIC")
    op.execute(
        """
        CREATE TRIGGER ledger_transactions_assign_posting_sequence
        BEFORE INSERT ON ledger_transactions
        FOR EACH ROW EXECUTE FUNCTION ledger_assign_posting_sequence()
        """
    )
    op.create_index(
        "ix_ledger_transactions_created",
        "ledger_transactions",
        ["created_at", "id"],
    )
    op.create_index(
        "ix_ledger_transactions_currency_type_created",
        "ledger_transactions",
        ["currency", "type", "created_at", "id"],
    )
    op.create_index(
        "ix_ledger_transactions_type_created",
        "ledger_transactions",
        ["type", "created_at", "id"],
    )
    op.create_index(
        "ix_ledger_transactions_source_created",
        "ledger_transactions",
        ["source_account_id", "created_at", "id"],
    )
    op.create_index(
        "ix_ledger_transactions_destination_created",
        "ledger_transactions",
        ["destination_account_id", "created_at", "id"],
    )

    op.create_table(
        "outbox_events",
        sa.Column(
            "id",
            sa.BigInteger(),
            nullable=False,
            autoincrement=False,
        ),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("aggregate_type", sa.String(length=32), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ("
            "'posting.created', "
            "'reversal.created', "
            "'request.replayed', "
            "'reconciliation.completed', "
            "'reconciliation.resolved'"
            ")",
            name="ck_outbox_events_event_type",
        ),
        sa.CheckConstraint(
            "aggregate_type IN ("
            "'ledger_transaction', "
            "'reconciliation_run', "
            "'reconciliation_item'"
            ")",
            name="ck_outbox_events_aggregate_type",
        ),
        sa.CheckConstraint(
            "request_id IS NULL OR "
            "(request_id = btrim(request_id) AND length(request_id) > 0)",
            name="ck_outbox_events_request_id",
        ),
        sa.CheckConstraint(
            "json_typeof(payload) = 'object'",
            name="ck_outbox_events_payload_object",
        ),
        sa.CheckConstraint(
            "octet_length(payload::text) <= 4096",
            name="ck_outbox_events_payload_size",
        ),
        sa.CheckConstraint("id > 0", name="ck_outbox_events_id_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
    )
    op.execute("CREATE SEQUENCE outbox_events_id_seq")
    op.execute("ALTER SEQUENCE outbox_events_id_seq OWNED BY outbox_events.id")
    op.execute(
        """
        CREATE FUNCTION outbox_assign_commit_order_id()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock(5814211102026082026);
            IF NEW.id IS NOT NULL THEN
                RAISE EXCEPTION 'outbox event id is database assigned'
                    USING ERRCODE = '23514';
            END IF;
            NEW.id := nextval('public.outbox_events_id_seq');
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION outbox_assign_commit_order_id() FROM PUBLIC")
    op.execute(
        """
        CREATE TRIGGER outbox_events_assign_commit_order_id
        BEFORE INSERT ON outbox_events
        FOR EACH ROW EXECUTE FUNCTION outbox_assign_commit_order_id()
        """
    )
    op.create_index(
        "ix_outbox_events_aggregate",
        "outbox_events",
        ["aggregate_type", "aggregate_id", "id"],
        unique=False,
    )
    op.execute(
        """
        CREATE TRIGGER outbox_events_are_immutable
        BEFORE UPDATE OR DELETE ON outbox_events
        FOR EACH ROW EXECUTE FUNCTION ledger_reject_mutation()
        """
    )

    op.create_table(
        "reconciliation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("fixture_key", sa.String(length=64), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="ck_reconciliation_runs_currency_code",
        ),
        sa.CheckConstraint(
            "period_start < period_end", name="ck_reconciliation_runs_period"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'completed')",
            name="ck_reconciliation_runs_status",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND completed_at IS NULL AND summary IS NULL) "
            "OR (status = 'completed' AND completed_at IS NOT NULL "
            "AND summary IS NOT NULL)",
            name="ck_reconciliation_runs_completion",
        ),
        sa.CheckConstraint(
            "summary IS NULL OR json_typeof(summary) = 'object'",
            name="ck_reconciliation_runs_summary_object",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reconciliation_runs"),
        sa.UniqueConstraint("fixture_key", name="uq_reconciliation_runs_fixture_key"),
    )

    op.create_table(
        "reconciliation_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_reference", sa.String(length=80), nullable=True),
        # Provider data is deliberately not a foreign key. A not-found claim is
        # a valid reconciliation exception that must remain representable.
        sa.Column(
            "claimed_transaction_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "matched_transaction_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("amount", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "result",
            sa.String(length=24),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("mismatch_code", sa.String(length=40), nullable=True),
        sa.Column(
            "resolution_status",
            sa.String(length=16),
            server_default=sa.text("'open'"),
            nullable=False,
        ),
        sa.Column("resolution_note", sa.String(length=240), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "amount > 0", name="ck_reconciliation_items_amount_positive"
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="ck_reconciliation_items_currency_code",
        ),
        sa.CheckConstraint(
            "result IN ("
            "'pending', 'matched', 'provider_only', 'ledger_only', "
            "'mismatched', 'duplicate'"
            ")",
            name="ck_reconciliation_items_result",
        ),
        sa.CheckConstraint(
            "mismatch_code IS NULL OR mismatch_code IN ("
            "'transaction_not_found', 'amount_mismatch', 'currency_mismatch', "
            "'transaction_type_mismatch', 'outside_period', 'duplicate_claim', "
            "'unclaimed_ledger_transaction'"
            ")",
            name="ck_reconciliation_items_mismatch_code",
        ),
        sa.CheckConstraint(
            "resolution_status IN ('open', 'matched', 'ignored')",
            name="ck_reconciliation_items_resolution_status",
        ),
        sa.CheckConstraint(
            "(result IN ('pending', 'matched') AND mismatch_code IS NULL) OR "
            "(result = 'provider_only' "
            "AND mismatch_code = 'transaction_not_found') OR "
            "(result = 'ledger_only' "
            "AND mismatch_code = 'unclaimed_ledger_transaction') OR "
            "(result = 'mismatched' AND mismatch_code IN ("
            "'amount_mismatch', 'currency_mismatch', "
            "'transaction_type_mismatch', 'outside_period'"
            ")) OR "
            "(result = 'duplicate' AND mismatch_code = 'duplicate_claim')",
            name="ck_reconciliation_items_result_mismatch",
        ),
        sa.CheckConstraint(
            "(result = 'ledger_only' AND provider_reference IS NULL) OR "
            "(result <> 'ledger_only' AND provider_reference IS NOT NULL)",
            name="ck_reconciliation_items_provider_reference",
        ),
        sa.CheckConstraint(
            "resolution_note IS NULL OR "
            "(resolution_note = btrim(resolution_note) "
            "AND length(resolution_note) > 0)",
            name="ck_reconciliation_items_resolution_note",
        ),
        sa.CheckConstraint(
            "resolution_status <> 'ignored' OR resolution_note IS NOT NULL",
            name="ck_reconciliation_items_ignore_reason",
        ),
        sa.CheckConstraint(
            "(resolution_status = 'open' AND resolved_at IS NULL) OR "
            "(resolution_status <> 'open' AND resolved_at IS NOT NULL)",
            name="ck_reconciliation_items_resolution_time",
        ),
        sa.CheckConstraint(
            "(result = 'matched' AND resolution_status = 'matched' "
            "AND matched_transaction_id IS NOT NULL) OR "
            "(result <> 'matched' AND matched_transaction_id IS NULL)",
            name="ck_reconciliation_items_match_evidence",
        ),
        sa.CheckConstraint(
            "resolution_status <> 'matched' OR result IN ('matched', 'ledger_only')",
            name="ck_reconciliation_items_matched_state",
        ),
        sa.CheckConstraint(
            "resolution_status <> 'ignored' OR result NOT IN ('pending', 'matched')",
            name="ck_reconciliation_items_ignored_state",
        ),
        sa.ForeignKeyConstraint(
            ["matched_transaction_id"],
            ["ledger_transactions.id"],
            name="fk_recon_items_matched_transaction",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["reconciliation_runs.id"],
            name="fk_recon_items_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reconciliation_items"),
    )
    op.create_index(
        "ix_reconciliation_items_run_result",
        "reconciliation_items",
        ["run_id", "result", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_reconciliation_items_run_resolution",
        "reconciliation_items",
        ["run_id", "resolution_status", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "uq_reconciliation_items_run_provider_reference",
        "reconciliation_items",
        ["run_id", "provider_reference"],
        unique=True,
        postgresql_where=sa.text("provider_reference IS NOT NULL"),
    )
    op.create_index(
        "uq_reconciliation_items_run_matched_transaction",
        "reconciliation_items",
        ["run_id", "matched_transaction_id"],
        unique=True,
        postgresql_where=sa.text("matched_transaction_id IS NOT NULL"),
    )
    op.create_index(
        "uq_reconciliation_items_run_ledger_transaction",
        "reconciliation_items",
        ["run_id", "claimed_transaction_id"],
        unique=True,
        postgresql_where=sa.text("result = 'ledger_only'"),
    )

    # The API identity cannot insert raw provider evidence. It can only ask this
    # narrowly-scoped function to derive a ledger-only exception from a pending
    # run and an eligible immutable transaction. The function owner is the
    # migration role; callers never receive table INSERT authority.
    op.execute(
        """
        CREATE FUNCTION reconciliation_insert_ledger_only(
            run_to_check uuid,
            transaction_to_check uuid,
            item_to_create uuid
        )
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            run_record public.reconciliation_runs%ROWTYPE;
            transaction_record public.ledger_transactions%ROWTYPE;
        BEGIN
            SELECT reconciliation_runs.*
            INTO run_record
            FROM public.reconciliation_runs
            WHERE reconciliation_runs.id = run_to_check
            FOR UPDATE;

            IF NOT FOUND OR run_record.status <> 'pending' THEN
                RAISE EXCEPTION 'ledger-only evidence requires a pending run'
                    USING ERRCODE = '23514';
            END IF;

            SELECT ledger_transactions.*
            INTO transaction_record
            FROM public.ledger_transactions
            WHERE ledger_transactions.id = transaction_to_check;

            IF NOT FOUND
                OR transaction_record.type NOT IN ('transfer', 'reversal')
                OR transaction_record.currency <> run_record.currency
                OR transaction_record.created_at < run_record.period_start
                OR transaction_record.created_at >= run_record.period_end
            THEN
                RAISE EXCEPTION 'transaction is ineligible for ledger-only evidence'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM public.reconciliation_items
                WHERE reconciliation_items.run_id = run_to_check
                    AND reconciliation_items.matched_transaction_id =
                        transaction_to_check
            ) THEN
                RAISE EXCEPTION 'matched transaction cannot be ledger-only'
                    USING ERRCODE = '23514';
            END IF;

            INSERT INTO public.reconciliation_items (
                id,
                run_id,
                provider_reference,
                claimed_transaction_id,
                matched_transaction_id,
                amount,
                currency,
                occurred_at,
                result,
                mismatch_code,
                resolution_status,
                resolution_note,
                created_at,
                resolved_at
            )
            VALUES (
                item_to_create,
                run_record.id,
                NULL,
                transaction_record.id,
                NULL,
                transaction_record.amount,
                transaction_record.currency,
                transaction_record.created_at,
                'ledger_only',
                'unclaimed_ledger_transaction',
                'open',
                NULL,
                transaction_timestamp(),
                NULL
            );
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "reconciliation_insert_ledger_only(uuid, uuid, uuid) FROM PUBLIC"
    )

    # Provider evidence is append-only. Runtime UPDATE authority is reserved
    # for one pending-to-completed classification and one open-to-resolved
    # operator decision; raw settlement fields never change in place.
    op.execute(
        """
        CREATE FUNCTION reconciliation_guard_run_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            expected_summary jsonb;
            provider_total numeric;
            ledger_total numeric;
        BEGIN
            IF ROW(
                NEW.id,
                NEW.provider,
                NEW.fixture_key,
                NEW.currency,
                NEW.period_start,
                NEW.period_end,
                NEW.created_at
            ) IS DISTINCT FROM ROW(
                OLD.id,
                OLD.provider,
                OLD.fixture_key,
                OLD.currency,
                OLD.period_start,
                OLD.period_end,
                OLD.created_at
            ) THEN
                RAISE EXCEPTION 'reconciliation run evidence is immutable'
                    USING ERRCODE = '23514';
            END IF;

            IF OLD.status = 'pending' THEN
                IF NEW.status <> 'completed'
                    OR NEW.summary IS NULL
                    OR NEW.completed_at IS NULL
                    OR EXISTS (
                        SELECT 1
                        FROM reconciliation_items
                        WHERE reconciliation_items.run_id = OLD.id
                            AND reconciliation_items.result = 'pending'
                    )
                THEN
                    RAISE EXCEPTION 'invalid reconciliation run transition'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF OLD.status = 'completed' THEN
                IF NEW.status <> 'completed'
                    OR NEW.completed_at IS DISTINCT FROM OLD.completed_at
                THEN
                    RAISE EXCEPTION 'completed reconciliation run is immutable'
                        USING ERRCODE = '23514';
                END IF;
            ELSE
                RAISE EXCEPTION 'invalid reconciliation run state'
                    USING ERRCODE = '23514';
            END IF;

            SELECT COALESCE(sum(reconciliation_items.amount), 0)
            INTO provider_total
            FROM reconciliation_items
            WHERE reconciliation_items.run_id = NEW.id
                AND reconciliation_items.provider_reference IS NOT NULL
                AND reconciliation_items.currency = NEW.currency;

            SELECT COALESCE(sum(ledger_transactions.amount), 0)
            INTO ledger_total
            FROM ledger_transactions
            WHERE ledger_transactions.currency = NEW.currency
                AND ledger_transactions.type IN ('transfer', 'reversal')
                AND ledger_transactions.created_at >= NEW.period_start
                AND ledger_transactions.created_at < NEW.period_end;

            SELECT jsonb_build_object(
                'counts', jsonb_build_object(
                    'matched', count(*) FILTER (WHERE result = 'matched'),
                    'provider_only', count(*) FILTER (
                        WHERE result = 'provider_only'
                    ),
                    'ledger_only', count(*) FILTER (WHERE result = 'ledger_only'),
                    'mismatched', count(*) FILTER (WHERE result = 'mismatched'),
                    'duplicate', count(*) FILTER (WHERE result = 'duplicate'),
                    'open_exceptions', count(*) FILTER (
                        WHERE result <> 'pending' AND resolution_status = 'open'
                    )
                ),
                'gross_volume', jsonb_build_object(
                    'currency', NEW.currency,
                    'provider_total', to_char(
                        provider_total,
                        'FM999999999999999999999999999999990.00'
                    ),
                    'ledger_total', to_char(
                        ledger_total,
                        'FM999999999999999999999999999999990.00'
                    ),
                    'difference', to_char(
                        provider_total - ledger_total,
                        'FM999999999999999999999999999999990.00'
                    )
                )
            )
            INTO expected_summary
            FROM reconciliation_items
            WHERE reconciliation_items.run_id = NEW.id;

            IF NEW.summary::jsonb IS DISTINCT FROM expected_summary THEN
                RAISE EXCEPTION 'reconciliation summary does not match evidence'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER reconciliation_runs_guard_updates
        BEFORE UPDATE ON reconciliation_runs
        FOR EACH ROW EXECUTE FUNCTION reconciliation_guard_run_update()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reconciliation_guard_item_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF ROW(
                NEW.id,
                NEW.run_id,
                NEW.provider_reference,
                NEW.claimed_transaction_id,
                NEW.amount,
                NEW.currency,
                NEW.occurred_at,
                NEW.created_at
            ) IS DISTINCT FROM ROW(
                OLD.id,
                OLD.run_id,
                OLD.provider_reference,
                OLD.claimed_transaction_id,
                OLD.amount,
                OLD.currency,
                OLD.occurred_at,
                OLD.created_at
            ) THEN
                RAISE EXCEPTION 'reconciliation item evidence is immutable'
                    USING ERRCODE = '23514';
            END IF;

            IF OLD.result = 'pending'
                AND OLD.resolution_status = 'open'
                AND OLD.resolved_at IS NULL
            THEN
                IF NEW.result = 'pending'
                    OR NEW.resolution_status = 'ignored'
                    OR NEW.resolution_note IS DISTINCT FROM OLD.resolution_note
                THEN
                    RAISE EXCEPTION 'invalid reconciliation classification'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF OLD.result <> 'pending'
                AND OLD.resolution_status = 'open'
            THEN
                IF NEW.resolution_status NOT IN ('matched', 'ignored') THEN
                    RAISE EXCEPTION 'invalid reconciliation resolution'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.resolution_status = 'ignored'
                    AND ROW(
                        NEW.result,
                        NEW.mismatch_code,
                        NEW.matched_transaction_id
                    ) IS DISTINCT FROM ROW(
                        OLD.result,
                        OLD.mismatch_code,
                        OLD.matched_transaction_id
                    )
                THEN
                    RAISE EXCEPTION 'ignored evidence cannot be reclassified'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.resolution_status = 'matched'
                    AND (
                        (
                            OLD.result = 'ledger_only'
                            AND ROW(
                                NEW.result,
                                NEW.mismatch_code,
                                NEW.matched_transaction_id
                            ) IS DISTINCT FROM ROW(
                                OLD.result,
                                OLD.mismatch_code,
                                OLD.matched_transaction_id
                            )
                        )
                        OR
                        (
                            OLD.result <> 'ledger_only'
                            AND (
                                NEW.result <> 'matched'
                                OR NEW.mismatch_code IS NOT NULL
                                OR NEW.matched_transaction_id IS NULL
                            )
                        )
                    )
                THEN
                    RAISE EXCEPTION 'invalid matched evidence transition'
                        USING ERRCODE = '23514';
                END IF;
            ELSE
                RAISE EXCEPTION 'resolved reconciliation item is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER reconciliation_items_guard_updates
        BEFORE UPDATE ON reconciliation_items
        FOR EACH ROW EXECUTE FUNCTION reconciliation_guard_item_update()
        """
    )

    # Row guards protect the transition being attempted. These deferred checks
    # protect the final cross-row state at commit: a pending run cannot retain
    # classified evidence, matched records must agree with the immutable ledger,
    # ledger-only counterparts must close together, and the stored summary must
    # be an exact projection of the evidence. This also makes a direct but
    # individually valid column UPDATE fail if its companion summary update is
    # omitted.
    op.execute(
        """
        CREATE FUNCTION reconciliation_assert_consistent(run_to_check uuid)
        RETURNS void
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            run_record public.reconciliation_runs%ROWTYPE;
            expected_summary jsonb;
            provider_total numeric;
            ledger_total numeric;
        BEGIN
            SELECT reconciliation_runs.*
            INTO run_record
            FROM public.reconciliation_runs
            WHERE reconciliation_runs.id = run_to_check;

            IF NOT FOUND THEN
                RETURN;
            END IF;

            IF run_record.status = 'pending' THEN
                IF run_record.summary IS NOT NULL
                    OR run_record.completed_at IS NOT NULL
                    OR EXISTS (
                        SELECT 1
                        FROM public.reconciliation_items
                        WHERE reconciliation_items.run_id = run_record.id
                            AND (
                                reconciliation_items.result <> 'pending'
                                OR reconciliation_items.mismatch_code IS NOT NULL
                                OR reconciliation_items.matched_transaction_id
                                    IS NOT NULL
                                OR reconciliation_items.resolution_status <> 'open'
                                OR reconciliation_items.resolution_note IS NOT NULL
                                OR reconciliation_items.resolved_at IS NOT NULL
                            )
                    )
                THEN
                    RAISE EXCEPTION
                        'pending reconciliation contains classified evidence'
                        USING ERRCODE = '23514';
                END IF;
                RETURN;
            END IF;

            IF run_record.status <> 'completed'
                OR run_record.summary IS NULL
                OR run_record.completed_at IS NULL
                OR EXISTS (
                    SELECT 1
                    FROM public.reconciliation_items
                    WHERE reconciliation_items.run_id = run_record.id
                        AND reconciliation_items.result = 'pending'
                )
            THEN
                RAISE EXCEPTION 'reconciliation completion is inconsistent'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM public.reconciliation_items AS item
                LEFT JOIN public.ledger_transactions AS claimed_transaction
                    ON claimed_transaction.id = item.claimed_transaction_id
                LEFT JOIN public.ledger_transactions AS matched_transaction
                    ON matched_transaction.id = item.matched_transaction_id
                WHERE item.run_id = run_record.id
                    AND (
                        (
                            item.result = 'matched'
                            AND NOT COALESCE(
                                matched_transaction.id IS NOT NULL
                                AND matched_transaction.type IN (
                                    'transfer', 'reversal'
                                )
                                AND matched_transaction.amount = item.amount
                                AND matched_transaction.currency = item.currency
                                AND item.currency = run_record.currency
                                AND item.occurred_at >= run_record.period_start
                                AND item.occurred_at < run_record.period_end
                                AND matched_transaction.created_at >=
                                    run_record.period_start
                                AND matched_transaction.created_at <
                                    run_record.period_end,
                                false
                            )
                        )
                        OR (
                            item.result = 'provider_only'
                            AND NOT (
                                item.occurred_at >= run_record.period_start
                                AND item.occurred_at < run_record.period_end
                                AND claimed_transaction.id IS NULL
                            )
                        )
                        OR (
                            item.result = 'duplicate'
                            AND NOT (
                                item.occurred_at >= run_record.period_start
                                AND item.occurred_at < run_record.period_end
                                AND item.claimed_transaction_id IS NOT NULL
                                AND EXISTS (
                                    SELECT 1
                                    FROM public.reconciliation_items AS duplicate
                                    WHERE duplicate.run_id = item.run_id
                                        AND duplicate.id <> item.id
                                        AND duplicate.claimed_transaction_id =
                                            item.claimed_transaction_id
                                )
                            )
                        )
                        OR (
                            item.result = 'mismatched'
                            AND NOT COALESCE(
                                CASE item.mismatch_code
                                    WHEN 'transaction_type_mismatch' THEN
                                        item.occurred_at >=
                                            run_record.period_start
                                        AND item.occurred_at < run_record.period_end
                                        AND claimed_transaction.id IS NOT NULL
                                        AND claimed_transaction.type NOT IN (
                                            'transfer', 'reversal'
                                        )
                                    WHEN 'currency_mismatch' THEN
                                        item.occurred_at >=
                                            run_record.period_start
                                        AND item.occurred_at < run_record.period_end
                                        AND claimed_transaction.id IS NOT NULL
                                        AND claimed_transaction.type IN (
                                            'transfer', 'reversal'
                                        )
                                        AND (
                                            item.currency <>
                                                run_record.currency
                                            OR claimed_transaction.currency <>
                                                run_record.currency
                                            OR claimed_transaction.currency <>
                                                item.currency
                                        )
                                    WHEN 'amount_mismatch' THEN
                                        item.occurred_at >=
                                            run_record.period_start
                                        AND item.occurred_at < run_record.period_end
                                        AND claimed_transaction.id IS NOT NULL
                                        AND claimed_transaction.type IN (
                                            'transfer', 'reversal'
                                        )
                                        AND item.currency = run_record.currency
                                        AND claimed_transaction.currency =
                                            run_record.currency
                                        AND claimed_transaction.amount <>
                                            item.amount
                                    WHEN 'outside_period' THEN
                                        item.occurred_at < run_record.period_start
                                        OR item.occurred_at >= run_record.period_end
                                        OR (
                                            item.occurred_at >=
                                                run_record.period_start
                                            AND item.occurred_at <
                                                run_record.period_end
                                            AND claimed_transaction.id IS NOT NULL
                                            AND claimed_transaction.type IN (
                                                'transfer', 'reversal'
                                            )
                                            AND item.currency =
                                                run_record.currency
                                            AND claimed_transaction.currency =
                                                run_record.currency
                                            AND claimed_transaction.amount =
                                                item.amount
                                            AND (
                                                claimed_transaction.created_at <
                                                    run_record.period_start
                                                OR claimed_transaction.created_at >=
                                                    run_record.period_end
                                            )
                                        )
                                    ELSE false
                                END,
                                false
                            )
                        )
                        OR (
                            item.result = 'ledger_only'
                            AND NOT COALESCE(
                                claimed_transaction.id IS NOT NULL
                                AND claimed_transaction.type IN (
                                    'transfer', 'reversal'
                                )
                                AND claimed_transaction.amount = item.amount
                                AND claimed_transaction.currency = item.currency
                                AND item.currency = run_record.currency
                                AND claimed_transaction.created_at =
                                    item.occurred_at
                                AND item.occurred_at >= run_record.period_start
                                AND item.occurred_at < run_record.period_end
                                AND (
                                    EXISTS (
                                        SELECT 1
                                        FROM public.reconciliation_items AS paired
                                        WHERE paired.run_id = item.run_id
                                            AND paired.result = 'matched'
                                            AND paired.matched_transaction_id =
                                                item.claimed_transaction_id
                                    )
                                ) = (item.resolution_status = 'matched'),
                                false
                            )
                        )
                        OR (
                            item.result = 'matched'
                            AND EXISTS (
                                SELECT 1
                                FROM public.reconciliation_items AS ledger_only
                                WHERE ledger_only.run_id = item.run_id
                                    AND ledger_only.result = 'ledger_only'
                                    AND ledger_only.claimed_transaction_id =
                                        item.matched_transaction_id
                                    AND ledger_only.resolution_status <> 'matched'
                            )
                        )
                    )
            )
            THEN
                RAISE EXCEPTION
                    'reconciliation classification contradicts ledger evidence'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM public.ledger_transactions AS eligible_transaction
                WHERE eligible_transaction.currency = run_record.currency
                    AND eligible_transaction.type IN ('transfer', 'reversal')
                    AND eligible_transaction.created_at >= run_record.period_start
                    AND eligible_transaction.created_at < run_record.period_end
                    AND NOT EXISTS (
                        SELECT 1
                        FROM public.reconciliation_items AS covered_item
                        WHERE covered_item.run_id = run_record.id
                            AND (
                                covered_item.matched_transaction_id =
                                    eligible_transaction.id
                                OR (
                                    covered_item.result = 'ledger_only'
                                    AND covered_item.claimed_transaction_id =
                                        eligible_transaction.id
                                )
                            )
                    )
            )
            THEN
                RAISE EXCEPTION
                    'reconciliation omits eligible ledger transactions'
                    USING ERRCODE = '23514';
            END IF;

            SELECT COALESCE(sum(reconciliation_items.amount), 0)
            INTO provider_total
            FROM public.reconciliation_items
            WHERE reconciliation_items.run_id = run_record.id
                AND reconciliation_items.provider_reference IS NOT NULL
                AND reconciliation_items.currency = run_record.currency;

            SELECT COALESCE(sum(ledger_transactions.amount), 0)
            INTO ledger_total
            FROM public.ledger_transactions
            WHERE ledger_transactions.currency = run_record.currency
                AND ledger_transactions.type IN ('transfer', 'reversal')
                AND ledger_transactions.created_at >= run_record.period_start
                AND ledger_transactions.created_at < run_record.period_end;

            SELECT jsonb_build_object(
                'counts', jsonb_build_object(
                    'matched', count(*) FILTER (WHERE result = 'matched'),
                    'provider_only', count(*) FILTER (
                        WHERE result = 'provider_only'
                    ),
                    'ledger_only', count(*) FILTER (
                        WHERE result = 'ledger_only'
                    ),
                    'mismatched', count(*) FILTER (
                        WHERE result = 'mismatched'
                    ),
                    'duplicate', count(*) FILTER (WHERE result = 'duplicate'),
                    'open_exceptions', count(*) FILTER (
                        WHERE result <> 'pending' AND resolution_status = 'open'
                    )
                ),
                'gross_volume', jsonb_build_object(
                    'currency', run_record.currency,
                    'provider_total', to_char(
                        provider_total,
                        'FM999999999999999999999999999999990.00'
                    ),
                    'ledger_total', to_char(
                        ledger_total,
                        'FM999999999999999999999999999999990.00'
                    ),
                    'difference', to_char(
                        provider_total - ledger_total,
                        'FM999999999999999999999999999999990.00'
                    )
                )
            )
            INTO expected_summary
            FROM public.reconciliation_items
            WHERE reconciliation_items.run_id = run_record.id;

            IF run_record.summary::jsonb IS DISTINCT FROM expected_summary THEN
                RAISE EXCEPTION 'reconciliation summary does not match evidence'
                    USING ERRCODE = '23514';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION reconciliation_validate_consistency()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            target_run_id uuid;
        BEGIN
            IF TG_TABLE_NAME = 'reconciliation_runs' THEN
                target_run_id := NEW.id;
            ELSIF TG_OP = 'DELETE' THEN
                target_run_id := OLD.run_id;
            ELSE
                target_run_id := NEW.run_id;
            END IF;
            PERFORM public.reconciliation_assert_consistent(target_run_id);
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION reconciliation_assert_consistent(uuid) FROM PUBLIC"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION reconciliation_validate_consistency() FROM PUBLIC"
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER reconciliation_runs_are_consistent
        AFTER INSERT OR UPDATE ON reconciliation_runs
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION reconciliation_validate_consistency()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER reconciliation_items_are_consistent
        AFTER INSERT OR UPDATE OR DELETE ON reconciliation_items
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION reconciliation_validate_consistency()
        """
    )

    # The deferred triggers installed by 0001 already call this function. The
    # replacement preserves the existing deposit/transfer rules and makes the
    # inverse posting plus original lineage properties of every reversal row.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ledger_assert_balanced(
            transaction_to_check uuid
        )
        RETURNS void
        LANGUAGE plpgsql
        AS $$
        DECLARE
            txn ledger_transactions%ROWTYPE;
            entry_count bigint;
            entry_total numeric;
            account_roles_match boolean;
            entries_match boolean;
            reversal_lineage_match boolean;
        BEGIN
            SELECT ledger_transactions.*
            INTO txn
            FROM ledger_transactions
            WHERE ledger_transactions.id = transaction_to_check;

            IF NOT FOUND THEN
                RETURN;
            END IF;

            SELECT EXISTS (
                SELECT 1
                FROM accounts AS source_account
                CROSS JOIN accounts AS destination_account
                WHERE source_account.id = txn.source_account_id
                    AND destination_account.id = txn.destination_account_id
                    AND source_account.currency = txn.currency
                    AND destination_account.currency = txn.currency
                    AND (
                        (
                            txn.type = 'deposit'
                            AND source_account.is_system = true
                            AND source_account.system_key =
                                'clearing:' || txn.currency
                            AND destination_account.is_system = false
                            AND destination_account.system_key IS NULL
                        )
                        OR
                        (
                            txn.type = 'transfer'
                            AND source_account.is_system = false
                            AND source_account.system_key IS NULL
                            AND destination_account.is_system = false
                            AND destination_account.system_key IS NULL
                        )
                        OR
                        (
                            txn.type = 'reversal'
                            AND source_account.is_system = false
                            AND source_account.system_key IS NULL
                            AND (
                                (
                                    destination_account.is_system = false
                                    AND destination_account.system_key IS NULL
                                )
                                OR
                                (
                                    destination_account.is_system = true
                                    AND destination_account.system_key =
                                        'clearing:' || txn.currency
                                )
                            )
                        )
                    )
            )
            INTO account_roles_match;

            IF txn.type = 'reversal' THEN
                SELECT EXISTS (
                    SELECT 1
                    FROM ledger_transactions AS original
                    WHERE original.id = txn.reverses_transaction_id
                        AND original.type IN ('deposit', 'transfer')
                        AND txn.source_account_id =
                            original.destination_account_id
                        AND txn.destination_account_id =
                            original.source_account_id
                        AND txn.amount = original.amount
                        AND txn.currency = original.currency
                        AND txn.created_at >= original.created_at
                )
                INTO reversal_lineage_match;
            ELSE
                reversal_lineage_match :=
                    txn.reverses_transaction_id IS NULL
                    AND txn.reversal_reason_code IS NULL
                    AND txn.reversal_note IS NULL;
            END IF;

            SELECT
                count(entry.id),
                COALESCE(sum(entry.amount), 0),
                (
                    count(entry.id) = 2
                    AND count(*) FILTER (
                        WHERE entry.sequence = 1
                    ) = 1
                    AND count(*) FILTER (
                        WHERE entry.sequence = 2
                    ) = 1
                    AND count(*) FILTER (
                        WHERE entry.currency IS DISTINCT FROM txn.currency
                            OR posting_account.currency
                                IS DISTINCT FROM txn.currency
                            OR entry.created_at
                                IS DISTINCT FROM txn.created_at
                    ) = 0
                    AND (
                        (
                            txn.type = 'deposit'
                            AND count(*) FILTER (
                                WHERE entry.sequence = 1
                                    AND entry.account_id =
                                        txn.destination_account_id
                                    AND entry.amount = txn.amount
                            ) = 1
                            AND count(*) FILTER (
                                WHERE entry.sequence = 2
                                    AND entry.account_id =
                                        txn.source_account_id
                                    AND entry.amount = -txn.amount
                            ) = 1
                        )
                        OR
                        (
                            txn.type IN ('transfer', 'reversal')
                            AND count(*) FILTER (
                                WHERE entry.sequence = 1
                                    AND entry.account_id =
                                        txn.source_account_id
                                    AND entry.amount = -txn.amount
                            ) = 1
                            AND count(*) FILTER (
                                WHERE entry.sequence = 2
                                    AND entry.account_id =
                                        txn.destination_account_id
                                    AND entry.amount = txn.amount
                            ) = 1
                        )
                    )
                )
            INTO entry_count, entry_total, entries_match
            FROM ledger_entries AS entry
            LEFT JOIN accounts AS posting_account
                ON posting_account.id = entry.account_id
            WHERE entry.transaction_id = txn.id;

            IF entry_count <> 2
                OR entry_total <> 0
                OR NOT COALESCE(account_roles_match, false)
                OR NOT COALESCE(entries_match, false)
                OR NOT COALESCE(reversal_lineage_match, false)
            THEN
                RAISE EXCEPTION
                    'ledger transaction % violates exact posting semantics: entries=%, total=%, account_roles_match=%, entries_match=%, reversal_lineage_match=%',
                    transaction_to_check,
                    entry_count,
                    entry_total,
                    account_roles_match,
                    entries_match,
                    reversal_lineage_match
                    USING ERRCODE = '23514';
            END IF;
        END;
        $$
        """
    )

    # Validate every immutable historical row under the replacement function.
    op.execute(
        """
        DO $$
        DECLARE
            historical_transaction_id uuid;
        BEGIN
            FOR historical_transaction_id IN
                SELECT ledger_transactions.id
                FROM ledger_transactions
                ORDER BY ledger_transactions.id
            LOOP
                PERFORM ledger_assert_balanced(historical_transaction_id);
            END LOOP;
        END;
        $$
        """
    )


def downgrade() -> None:
    # Revision 0002 cannot represent compensating reversals. Refuse the entire
    # transactional downgrade before dropping any 0003 object rather than
    # silently deleting immutable financial history.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM ledger_transactions
                WHERE type = 'reversal'
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade to 0002 while reversal rows exist'
                    USING
                        ERRCODE = '55000',
                        HINT = 'Retain revision 0003 or archive the database.';
            END IF;
        END;
        $$
        """
    )

    # Restore 0002's exact deposit/transfer assertion while reversal columns are
    # still present. Adding the old type check later will safely refuse a lossy
    # downgrade if reversal rows have already been posted.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ledger_assert_balanced(
            transaction_to_check uuid
        )
        RETURNS void
        LANGUAGE plpgsql
        AS $$
        DECLARE
            txn ledger_transactions%ROWTYPE;
            entry_count bigint;
            entry_total numeric;
            account_roles_match boolean;
            entries_match boolean;
        BEGIN
            SELECT ledger_transactions.*
            INTO txn
            FROM ledger_transactions
            WHERE ledger_transactions.id = transaction_to_check;

            IF NOT FOUND THEN
                RETURN;
            END IF;

            SELECT EXISTS (
                SELECT 1
                FROM accounts AS source_account
                CROSS JOIN accounts AS destination_account
                WHERE source_account.id = txn.source_account_id
                    AND destination_account.id = txn.destination_account_id
                    AND source_account.currency = txn.currency
                    AND destination_account.currency = txn.currency
                    AND (
                        (
                            txn.type = 'deposit'
                            AND source_account.is_system = true
                            AND source_account.system_key =
                                'clearing:' || txn.currency
                            AND destination_account.is_system = false
                            AND destination_account.system_key IS NULL
                        )
                        OR
                        (
                            txn.type = 'transfer'
                            AND source_account.is_system = false
                            AND source_account.system_key IS NULL
                            AND destination_account.is_system = false
                            AND destination_account.system_key IS NULL
                        )
                    )
            )
            INTO account_roles_match;

            SELECT
                count(entry.id),
                COALESCE(sum(entry.amount), 0),
                (
                    count(entry.id) = 2
                    AND count(*) FILTER (
                        WHERE entry.sequence = 1
                    ) = 1
                    AND count(*) FILTER (
                        WHERE entry.sequence = 2
                    ) = 1
                    AND count(*) FILTER (
                        WHERE entry.currency IS DISTINCT FROM txn.currency
                            OR posting_account.currency
                                IS DISTINCT FROM txn.currency
                            OR entry.created_at
                                IS DISTINCT FROM txn.created_at
                    ) = 0
                    AND (
                        (
                            txn.type = 'deposit'
                            AND count(*) FILTER (
                                WHERE entry.sequence = 1
                                    AND entry.account_id =
                                        txn.destination_account_id
                                    AND entry.amount = txn.amount
                            ) = 1
                            AND count(*) FILTER (
                                WHERE entry.sequence = 2
                                    AND entry.account_id =
                                        txn.source_account_id
                                    AND entry.amount = -txn.amount
                            ) = 1
                        )
                        OR
                        (
                            txn.type = 'transfer'
                            AND count(*) FILTER (
                                WHERE entry.sequence = 1
                                    AND entry.account_id =
                                        txn.source_account_id
                                    AND entry.amount = -txn.amount
                            ) = 1
                            AND count(*) FILTER (
                                WHERE entry.sequence = 2
                                    AND entry.account_id =
                                        txn.destination_account_id
                                    AND entry.amount = txn.amount
                            ) = 1
                        )
                    )
                )
            INTO entry_count, entry_total, entries_match
            FROM ledger_entries AS entry
            LEFT JOIN accounts AS posting_account
                ON posting_account.id = entry.account_id
            WHERE entry.transaction_id = txn.id;

            IF entry_count <> 2
                OR entry_total <> 0
                OR NOT COALESCE(account_roles_match, false)
                OR NOT COALESCE(entries_match, false)
            THEN
                RAISE EXCEPTION
                    'ledger transaction % violates exact posting semantics: entries=%, total=%, account_roles_match=%, entries_match=%',
                    transaction_to_check,
                    entry_count,
                    entry_total,
                    account_roles_match,
                    entries_match
                    USING ERRCODE = '23514';
            END IF;
        END;
        $$
        """
    )

    op.execute(
        "DROP TRIGGER reconciliation_items_are_consistent ON reconciliation_items"
    )
    op.execute("DROP TRIGGER reconciliation_runs_are_consistent ON reconciliation_runs")
    op.execute("DROP FUNCTION reconciliation_validate_consistency()")
    op.execute("DROP FUNCTION reconciliation_assert_consistent(uuid)")
    op.execute(
        "DROP TRIGGER reconciliation_items_guard_updates ON reconciliation_items"
    )
    op.execute("DROP FUNCTION reconciliation_guard_item_update()")
    op.execute("DROP TRIGGER reconciliation_runs_guard_updates ON reconciliation_runs")
    op.execute("DROP FUNCTION reconciliation_guard_run_update()")
    op.execute("DROP FUNCTION reconciliation_insert_ledger_only(uuid, uuid, uuid)")

    op.drop_index(
        "uq_reconciliation_items_run_ledger_transaction",
        table_name="reconciliation_items",
    )

    op.drop_index(
        "uq_reconciliation_items_run_matched_transaction",
        table_name="reconciliation_items",
    )
    op.drop_index(
        "uq_reconciliation_items_run_provider_reference",
        table_name="reconciliation_items",
    )
    op.drop_index(
        "ix_reconciliation_items_run_result", table_name="reconciliation_items"
    )
    op.drop_index(
        "ix_reconciliation_items_run_resolution",
        table_name="reconciliation_items",
    )
    op.drop_table("reconciliation_items")
    op.drop_table("reconciliation_runs")

    op.execute("DROP TRIGGER outbox_events_assign_commit_order_id ON outbox_events")
    op.execute("DROP FUNCTION outbox_assign_commit_order_id()")
    op.execute("DROP TRIGGER outbox_events_are_immutable ON outbox_events")
    op.drop_index("ix_outbox_events_aggregate", table_name="outbox_events")
    op.execute("ALTER SEQUENCE outbox_events_id_seq OWNED BY NONE")
    op.drop_table("outbox_events")
    op.execute("DROP SEQUENCE outbox_events_id_seq")

    op.drop_index(
        "ix_ledger_transactions_destination_created",
        table_name="ledger_transactions",
    )
    op.drop_index(
        "ix_ledger_transactions_source_created",
        table_name="ledger_transactions",
    )
    op.drop_index(
        "ix_ledger_transactions_type_created",
        table_name="ledger_transactions",
    )
    op.drop_index(
        "ix_ledger_transactions_currency_type_created",
        table_name="ledger_transactions",
    )
    op.drop_index(
        "ix_ledger_transactions_created",
        table_name="ledger_transactions",
    )

    op.execute(
        "DROP TRIGGER ledger_transactions_assign_posting_sequence "
        "ON ledger_transactions"
    )
    op.execute("DROP FUNCTION ledger_assign_posting_sequence()")
    op.drop_constraint(
        "uq_ledger_transactions_posting_sequence",
        "ledger_transactions",
        type_="unique",
    )
    op.execute("ALTER SEQUENCE ledger_transactions_posting_sequence_seq OWNED BY NONE")
    op.drop_column("ledger_transactions", "posting_sequence")
    op.execute("DROP SEQUENCE ledger_transactions_posting_sequence_seq")

    op.drop_constraint(
        "ck_ledger_transactions_not_self_reversal",
        "ledger_transactions",
        type_="check",
    )
    op.drop_constraint(
        "ck_ledger_transactions_reversal_note",
        "ledger_transactions",
        type_="check",
    )
    op.drop_constraint(
        "ck_ledger_transactions_reversal_reason",
        "ledger_transactions",
        type_="check",
    )
    op.drop_constraint(
        "ck_ledger_transactions_reversal_fields",
        "ledger_transactions",
        type_="check",
    )
    op.drop_constraint(
        "uq_ledger_transactions_reverses_transaction_id",
        "ledger_transactions",
        type_="unique",
    )
    op.drop_constraint(
        "fk_ledger_tx_reverses_ledger_tx",
        "ledger_transactions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_ledger_transactions_idempotency_pair",
        "ledger_transactions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_ledger_transactions_idempotency_pair",
        "ledger_transactions",
        """
        (
            type = 'deposit'
            AND
            (
                (idempotency_key IS NULL AND request_fingerprint IS NULL)
                OR
                (idempotency_key IS NOT NULL AND request_fingerprint IS NOT NULL)
            )
        )
        OR
        (
            type = 'transfer'
            AND idempotency_key IS NOT NULL
            AND request_fingerprint IS NOT NULL
        )
        """,
    )
    op.drop_constraint(
        "ck_ledger_transactions_type", "ledger_transactions", type_="check"
    )
    op.create_check_constraint(
        "ck_ledger_transactions_type",
        "ledger_transactions",
        "type IN ('deposit', 'transfer')",
    )
    op.drop_column("ledger_transactions", "reversal_note")
    op.drop_column("ledger_transactions", "reversal_reason_code")
    op.drop_column("ledger_transactions", "reverses_transaction_id")

    op.drop_constraint("ck_accounts_display_name", "accounts", type_="check")
    op.drop_column("accounts", "display_name")
