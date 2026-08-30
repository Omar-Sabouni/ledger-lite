"""Create the immutable double-entry ledger.

Revision ID: 0001
Revises:
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "is_system", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("system_key", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_accounts_currency_code"),
        sa.PrimaryKeyConstraint("id", name="pk_accounts"),
        sa.UniqueConstraint("system_key", name="uq_accounts_system_key"),
    )

    op.create_table(
        "ledger_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("source_account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "destination_account_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("response_payload", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name="ck_ledger_transactions_amount_positive"),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="ck_ledger_transactions_currency_code",
        ),
        sa.CheckConstraint(
            "type IN ('deposit', 'transfer')",
            name="ck_ledger_transactions_type",
        ),
        sa.ForeignKeyConstraint(
            ["destination_account_id"],
            ["accounts.id"],
            name="fk_ledger_transactions_destination_account_id_accounts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_account_id"],
            ["accounts.id"],
            name="fk_ledger_transactions_source_account_id_accounts",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ledger_transactions"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_ledger_transactions_idempotency_key"
        ),
    )

    op.create_table(
        "ledger_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.SmallInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("amount <> 0", name="ck_ledger_entries_amount_nonzero"),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'", name="ck_ledger_entries_currency_code"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_ledger_entries_account_id_accounts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["ledger_transactions.id"],
            name="fk_ledger_entries_transaction_id_ledger_transactions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ledger_entries"),
        sa.UniqueConstraint(
            "transaction_id", "sequence", name="uq_ledger_entries_transaction_sequence"
        ),
    )
    op.create_index(
        "ix_ledger_entries_account_created_at",
        "ledger_entries",
        ["account_id", "created_at"],
        unique=False,
    )

    # Ledger rows can only be inserted. Corrections are represented by new,
    # reversing transactions, preserving a complete audit trail.
    op.execute(
        """
        CREATE FUNCTION ledger_reject_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME
                USING ERRCODE = '23514';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER ledger_transactions_are_immutable
        BEFORE UPDATE OR DELETE ON ledger_transactions
        FOR EACH ROW EXECUTE FUNCTION ledger_reject_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER ledger_entries_are_immutable
        BEFORE UPDATE OR DELETE ON ledger_entries
        FOR EACH ROW EXECUTE FUNCTION ledger_reject_mutation()
        """
    )
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

    # This assertion runs at COMMIT, after both sides of a transaction have
    # been inserted. Besides SUM(entries.amount) = 0 it verifies that every
    # posting uses the transaction/account currency.
    op.execute(
        """
        CREATE FUNCTION ledger_assert_balanced(transaction_to_check uuid)
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
    op.execute(
        """
        CREATE FUNCTION ledger_check_transaction_row()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            PERFORM ledger_assert_balanced(NEW.id);
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION ledger_check_entry_row()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                PERFORM ledger_assert_balanced(OLD.transaction_id);
            ELSE
                PERFORM ledger_assert_balanced(NEW.transaction_id);
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER ledger_transaction_must_balance
        AFTER INSERT ON ledger_transactions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION ledger_check_transaction_row()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER ledger_entry_must_balance
        AFTER INSERT OR UPDATE OR DELETE ON ledger_entries
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION ledger_check_entry_row()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS ledger_entry_must_balance ON ledger_entries")
    op.execute(
        "DROP TRIGGER IF EXISTS ledger_transaction_must_balance ON ledger_transactions"
    )
    op.execute("DROP FUNCTION IF EXISTS ledger_check_entry_row()")
    op.execute("DROP FUNCTION IF EXISTS ledger_check_transaction_row()")
    op.execute("DROP FUNCTION IF EXISTS ledger_assert_balanced(uuid)")
    op.execute("DROP TRIGGER IF EXISTS account_currency_is_immutable ON accounts")
    op.execute("DROP FUNCTION IF EXISTS ledger_reject_account_currency_change()")
    op.execute("DROP TRIGGER IF EXISTS ledger_entries_are_immutable ON ledger_entries")
    op.execute(
        "DROP TRIGGER IF EXISTS ledger_transactions_are_immutable ON ledger_transactions"
    )
    op.execute("DROP FUNCTION IF EXISTS ledger_reject_mutation()")
    op.drop_index("ix_ledger_entries_account_created_at", table_name="ledger_entries")
    op.drop_table("ledger_entries")
    op.drop_table("ledger_transactions")
    op.drop_table("accounts")
