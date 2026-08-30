"""Harden ledger transaction and posting semantics.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_and_validate_check(
    table_name: str, constraint_name: str, condition: str
) -> None:
    """Add a PostgreSQL CHECK without a table scan, then validate it."""

    op.execute(
        sa.text(
            f"ALTER TABLE {table_name} "
            f"ADD CONSTRAINT {constraint_name} "
            f"CHECK ({condition}) NOT VALID"
        )
    )
    op.execute(
        sa.text(f"ALTER TABLE {table_name} VALIDATE CONSTRAINT {constraint_name}")
    )


def upgrade() -> None:
    # A system account is the one clearing account for its currency. Customer
    # accounts have no system identity. NOT VALID followed by VALIDATE keeps the
    # constraint safe for existing installations while reducing the duration of
    # the strongest table lock.
    _add_and_validate_check(
        "accounts",
        "ck_accounts_system_identity",
        """
        (
            is_system = false
            AND system_key IS NULL
        )
        OR
        (
            is_system = true
            AND system_key IS NOT NULL
            AND system_key = 'clearing:' || currency
        )
        """,
    )

    # All supported transactions have two explicit participants and a stored
    # result. Rows written by 0001 already populate these fields.
    op.alter_column(
        "ledger_transactions",
        "source_account_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.alter_column(
        "ledger_transactions",
        "destination_account_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.alter_column(
        "ledger_transactions",
        "response_payload",
        existing_type=sa.JSON(),
        nullable=False,
    )

    _add_and_validate_check(
        "ledger_transactions",
        "ck_ledger_transactions_distinct_accounts",
        "source_account_id <> destination_account_id",
    )
    _add_and_validate_check(
        "ledger_transactions",
        "ck_ledger_transactions_idempotency_pair",
        """
        (
            type = 'deposit'
            AND
            (
                (
                    idempotency_key IS NULL
                    AND request_fingerprint IS NULL
                )
                OR
                (
                    idempotency_key IS NOT NULL
                    AND request_fingerprint IS NOT NULL
                )
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
    _add_and_validate_check(
        "ledger_transactions",
        "ck_ledger_transactions_request_fingerprint_shape",
        """
        request_fingerprint IS NULL
        OR request_fingerprint ~ '^[0-9a-f]{64}$'
        """,
    )
    _add_and_validate_check(
        "ledger_entries",
        "ck_ledger_entries_sequence",
        "sequence IN (1, 2)",
    )

    # Currency, role, and system identity jointly define an account's place in
    # the ledger, so none may change after creation.
    op.execute("DROP TRIGGER account_currency_is_immutable ON accounts")
    op.execute("DROP FUNCTION ledger_reject_account_currency_change()")
    op.execute(
        """
        CREATE FUNCTION ledger_reject_account_identity_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.currency IS DISTINCT FROM OLD.currency
                OR NEW.is_system IS DISTINCT FROM OLD.is_system
                OR NEW.system_key IS DISTINCT FROM OLD.system_key
            THEN
                RAISE EXCEPTION 'account identity is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER account_identity_is_immutable
        BEFORE UPDATE OF currency, is_system, system_key ON accounts
        FOR EACH ROW EXECUTE FUNCTION ledger_reject_account_identity_change()
        """
    )

    # The deferred triggers created by 0001 continue to call this function.
    # Exact posting checks make balance, participant, direction, currency, and
    # timestamp agreement properties of the database rather than conventions in
    # the service layer.
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

    # Replacing a trigger function only protects future trigger executions.
    # Explicitly apply the stronger assertion to every transaction already in
    # the ledger before the migration can commit.
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

    op.drop_index("ix_ledger_entries_account_created_at", table_name="ledger_entries")
    op.create_index(
        "ix_ledger_entries_account_statement",
        "ledger_entries",
        ["account_id", "created_at", "transaction_id", "sequence"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ledger_entries_account_statement", table_name="ledger_entries")
    op.create_index(
        "ix_ledger_entries_account_created_at",
        "ledger_entries",
        ["account_id", "created_at"],
        unique=False,
    )

    # Restore the original 0001 balance-and-currency assertion exactly.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ledger_assert_balanced(
            transaction_to_check uuid
        )
        RETURNS void
        LANGUAGE plpgsql
        AS $$
        DECLARE
            entry_count bigint;
            entry_total numeric;
            currencies_match boolean;
        BEGIN
            SELECT
                count(entry.id),
                COALESCE(sum(entry.amount), 0),
                COALESCE(
                    bool_and(
                        entry.currency = txn.currency
                        AND account.currency = txn.currency
                        AND (
                            txn.source_account_id IS NULL
                            OR source_account.currency = txn.currency
                        )
                        AND (
                            txn.destination_account_id IS NULL
                            OR destination_account.currency = txn.currency
                        )
                    ),
                    false
                )
            INTO entry_count, entry_total, currencies_match
            FROM ledger_transactions AS txn
            LEFT JOIN ledger_entries AS entry
                ON entry.transaction_id = txn.id
            LEFT JOIN accounts AS account
                ON account.id = entry.account_id
            LEFT JOIN accounts AS source_account
                ON source_account.id = txn.source_account_id
            LEFT JOIN accounts AS destination_account
                ON destination_account.id = txn.destination_account_id
            WHERE txn.id = transaction_to_check
            GROUP BY txn.id;

            IF NOT FOUND THEN
                RETURN;
            END IF;

            IF entry_count <> 2 OR entry_total <> 0 OR NOT currencies_match THEN
                RAISE EXCEPTION
                    'ledger transaction % is invalid: entries=%, total=%, currencies_match=%',
                    transaction_to_check, entry_count, entry_total, currencies_match
                    USING ERRCODE = '23514';
            END IF;
        END;
        $$
        """
    )

    op.execute("DROP TRIGGER account_identity_is_immutable ON accounts")
    op.execute("DROP FUNCTION ledger_reject_account_identity_change()")
    op.execute(
        """
        CREATE FUNCTION ledger_reject_account_currency_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.currency IS DISTINCT FROM OLD.currency THEN
                RAISE EXCEPTION 'account currency is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER account_currency_is_immutable
        BEFORE UPDATE OF currency ON accounts
        FOR EACH ROW EXECUTE FUNCTION ledger_reject_account_currency_change()
        """
    )

    op.drop_constraint("ck_ledger_entries_sequence", "ledger_entries", type_="check")
    op.drop_constraint(
        "ck_ledger_transactions_request_fingerprint_shape",
        "ledger_transactions",
        type_="check",
    )
    op.drop_constraint(
        "ck_ledger_transactions_idempotency_pair",
        "ledger_transactions",
        type_="check",
    )
    op.drop_constraint(
        "ck_ledger_transactions_distinct_accounts",
        "ledger_transactions",
        type_="check",
    )

    op.alter_column(
        "ledger_transactions",
        "response_payload",
        existing_type=sa.JSON(),
        nullable=True,
    )
    op.alter_column(
        "ledger_transactions",
        "destination_account_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.alter_column(
        "ledger_transactions",
        "source_account_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.drop_constraint("ck_accounts_system_identity", "accounts", type_="check")
