from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, null, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import services
from app.database import SessionLocal
from app.ledger import append_outbox_event
from app.models import (
    Account,
    LedgerEntry,
    LedgerTransaction,
    OutboxEvent,
    ReconciliationItem,
    ReconciliationRun,
)
from app.seed import RECONCILIATION_RUN_ID, TREASURY_ACCOUNT_ID, seed

pytestmark = pytest.mark.integration


def _create_account(client: TestClient, name: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/accounts",
        headers={"Idempotency-Key": f"create-{uuid4()}"},
        json={"currency": "AED", "display_name": name},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _deposit(
    client: TestClient, account_id: str, amount: str, key: str
) -> dict[str, object]:
    response = client.post(
        f"/api/v1/accounts/{account_id}/deposits",
        headers={"Idempotency-Key": key},
        json={"amount": amount},
    )
    assert response.status_code == 201, response.text
    assert response.headers["Idempotent-Replayed"] == "false"
    return response.json()


def _transfer(
    client: TestClient,
    source_id: str,
    destination_id: str,
    amount: str,
    key: str,
) -> dict[str, object]:
    response = client.post(
        "/api/v1/transfers",
        headers={"Idempotency-Key": key},
        json={
            "source_account_id": source_id,
            "destination_account_id": destination_id,
            "amount": amount,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_account_creation_is_idempotent_and_conflict_safe(
    client: TestClient, db_session: Session
) -> None:
    assert db_session.scalar(text("SHOW transaction_isolation")) == "read committed"
    headers = {"Idempotency-Key": "create-account-retry-proof"}
    payload = {"currency": "AED", "display_name": "Retry-safe account"}

    first = client.post("/api/v1/accounts", headers=headers, json=payload)
    replay = client.post("/api/v1/accounts", headers=headers, json=payload)
    conflict = client.post(
        "/api/v1/accounts",
        headers=headers,
        json={"currency": "AED", "display_name": "Different intent"},
    )

    assert first.status_code == replay.status_code == 201
    assert first.headers["Idempotent-Replayed"] == "false"
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json() == first.json()
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"
    assert db_session.scalar(select(func.count()).select_from(Account)) == 1

    account_key_reused_for_money = client.post(
        f"/api/v1/accounts/{first.json()['id']}/deposits",
        headers=headers,
        json={"amount": "1.00"},
    )
    assert account_key_reused_for_money.status_code == 409

    funding = _create_account(client, "Global key registry funding")
    _deposit(
        client,
        str(funding["id"]),
        "5.00",
        "movement-key-cannot-create-account",
    )
    money_key_reused_for_account = client.post(
        "/api/v1/accounts",
        headers={"Idempotency-Key": "movement-key-cannot-create-account"},
        json={"currency": "AED", "display_name": "Must conflict"},
    )
    assert money_key_reused_for_account.status_code == 409

    barrier = Barrier(2)

    def concurrent_create() -> tuple[str, bool]:
        barrier.wait(timeout=5)
        with SessionLocal() as session:
            result = services.create_account(
                session,
                "AED",
                display_name="Concurrent retry account",
                idempotency_key="concurrent-create-retry-proof",
            )
            return str(result.payload["id"]), result.replayed

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: concurrent_create(), range(2)))

    assert len({account_id for account_id, _replayed in results}) == 1
    assert {replayed for _account_id, replayed in results} == {False, True}


def test_console_read_models_paginate_and_return_sanitized_problems(
    client: TestClient,
) -> None:
    seed()

    overview = client.get("/api/v1/overview")
    assert overview.status_code == 200, overview.text
    assert overview.json()["integrity"] == {
        "transaction_count": 10,
        "entry_count": 20,
        "reversal_count": 1,
        "unbalanced_transaction_count": 0,
        "replay_count": 0,
        "open_reconciliation_exceptions": 0,
    }
    aed = overview.json()["currencies"][0]
    assert aed["currency"] == "AED"
    assert aed["total_customer_funds"] == "500000.00"
    assert aed["net_imbalance"] == "0.00"

    first_accounts = client.get("/api/v1/accounts?limit=2").json()
    assert len(first_accounts["items"]) == 2
    assert first_accounts["next_cursor"]
    second_accounts = client.get(
        "/api/v1/accounts",
        params={"limit": 2, "cursor": first_accounts["next_cursor"]},
    ).json()
    account_ids = {
        item["id"] for item in first_accounts["items"] + second_accounts["items"]
    }
    assert len(account_ids) == 4

    first_transactions = client.get("/api/v1/transactions?limit=4").json()
    second_transactions = client.get(
        "/api/v1/transactions",
        params={"limit": 4, "cursor": first_transactions["next_cursor"]},
    ).json()
    first_ids = {item["id"] for item in first_transactions["items"]}
    second_ids = {item["id"] for item in second_transactions["items"]}
    assert first_ids.isdisjoint(second_ids)

    invalid = client.post(
        f"/api/v1/accounts/{TREASURY_ACCOUNT_ID}/deposits",
        headers={
            "Idempotency-Key": "invalid-float",
            "X-Request-ID": "console-contract-check",
        },
        json={"amount": 1.25},
    )
    assert invalid.status_code == 422
    assert invalid.headers["content-type"].startswith("application/problem+json")
    assert invalid.headers["X-Request-ID"] == "console-contract-check"
    assert invalid.headers["cache-control"].startswith("private, no-store")
    assert invalid.headers["x-content-type-options"] == "nosniff"
    assert invalid.json()["code"] == "validation_error"
    assert "1.25" not in invalid.text


def test_reversal_posts_an_exact_idempotent_compensation(
    client: TestClient, db_session: Session
) -> None:
    source = _create_account(client, "Reversal Source")
    destination = _create_account(client, "Reversal Destination")
    _deposit(client, str(source["id"]), "100.00", "reversal-funding")
    original = _transfer(
        client,
        str(source["id"]),
        str(destination["id"]),
        "30.00",
        "reversal-transfer",
    )

    path = f"/api/v1/transactions/{original['transaction_id']}/reversals"
    headers = {"Idempotency-Key": "reversal-once"}
    body = {"reason_code": "operator_correction", "note": "Duplicate batch"}
    posted = client.post(path, headers=headers, json=body)
    replayed = client.post(path, headers=headers, json=body)
    duplicate = client.post(
        path,
        headers={"Idempotency-Key": "reversal-twice"},
        json=body,
    )

    assert posted.status_code == 201, posted.text
    assert posted.headers["Idempotent-Replayed"] == "false"
    assert replayed.status_code == 201, replayed.text
    assert replayed.headers["Idempotent-Replayed"] == "true"
    assert replayed.json() == posted.json()
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "transaction_already_reversed"

    detail = client.get(f"/api/v1/transactions/{original['transaction_id']}").json()
    assert detail["reversed_by_transaction_id"] == posted.json()["transaction_id"]
    assert detail["integrity"] == {
        "entry_count": 2,
        "posting_sum": "0.00",
        "balanced": True,
        "currency_consistent": True,
    }
    assert (
        client.get(f"/api/v1/accounts/{source['id']}/statement").json()["balance"]
        == "100.00"
    )
    assert (
        client.get(f"/api/v1/accounts/{destination['id']}/statement").json()["balance"]
        == "0.00"
    )

    db_session.expire_all()
    reversal_id = UUID(posted.json()["transaction_id"])
    postings = db_session.scalars(
        select(LedgerEntry)
        .where(LedgerEntry.transaction_id == reversal_id)
        .order_by(LedgerEntry.sequence)
    ).all()
    assert [posting.amount for posting in postings] == [
        Decimal("-30.00"),
        Decimal("30.00"),
    ]
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(LedgerTransaction)
            .where(
                LedgerTransaction.reverses_transaction_id
                == UUID(original["transaction_id"])
            )
        )
        == 1
    )


def test_reconciliation_classifies_and_resolves_without_mutating_ledger(
    client: TestClient, db_session: Session
) -> None:
    seed()
    before = (
        db_session.scalar(select(func.count()).select_from(LedgerTransaction)),
        db_session.scalar(select(func.count()).select_from(LedgerEntry)),
    )

    executed = client.post(
        f"/api/v1/reconciliation/runs/{RECONCILIATION_RUN_ID}/execute"
    )
    assert executed.status_code == 200, executed.text
    counts = executed.json()["summary"]["counts"]
    assert counts == {
        "matched": 4,
        "provider_only": 1,
        "ledger_only": 2,
        "mismatched": 2,
        "duplicate": 1,
        "open_exceptions": 6,
    }

    items = client.get(
        f"/api/v1/reconciliation/runs/{RECONCILIATION_RUN_ID}/items?limit=100"
    ).json()["items"]
    assert len(items) == 10
    automatically_matched = next(item for item in items if item["result"] == "matched")
    not_a_manual_replay = client.post(
        f"/api/v1/reconciliation/items/{automatically_matched['id']}/match",
        json={"transaction_id": automatically_matched["matched_transaction_id"]},
    )
    assert not_a_manual_replay.status_code == 409
    assert not_a_manual_replay.json()["code"] == "reconciliation_item_resolved"
    provider_only = next(item for item in items if item["result"] == "provider_only")
    ignored = client.post(
        f"/api/v1/reconciliation/items/{provider_only['id']}/ignore",
        json={"reason": "Confirmed synthetic provider exception"},
    )
    assert ignored.status_code == 200, ignored.text
    assert ignored.json()["resolution_status"] == "ignored"
    events_after_resolution = db_session.scalar(
        select(func.count()).select_from(OutboxEvent)
    )
    ignored_replay = client.post(
        f"/api/v1/reconciliation/items/{provider_only['id']}/ignore",
        json={"reason": "Confirmed synthetic provider exception"},
    )
    assert ignored_replay.status_code == 200, ignored_replay.text
    assert ignored_replay.json() == ignored.json()
    assert (
        db_session.scalar(select(func.count()).select_from(OutboxEvent))
        == events_after_resolution
    )
    assert (
        client.get("/api/v1/overview").json()["integrity"][
            "open_reconciliation_exceptions"
        ]
        == 5
    )

    # Database guards protect both raw processor evidence and the derived audit
    # summary even if a future code path bypasses the service transition rules.
    with SessionLocal() as mutation_session:
        for statement in (
            update(ReconciliationRun)
            .where(ReconciliationRun.id == RECONCILIATION_RUN_ID)
            .values(provider="Tampered processor"),
            update(ReconciliationRun)
            .where(ReconciliationRun.id == RECONCILIATION_RUN_ID)
            .values(summary={"counts": {}, "gross_volume": {}}),
            update(ReconciliationItem)
            .where(ReconciliationItem.id == UUID(provider_only["id"]))
            .values(amount=Decimal("999.00")),
            update(ReconciliationItem)
            .where(ReconciliationItem.id == UUID(provider_only["id"]))
            .values(resolution_note="Rewritten after resolution"),
        ):
            with pytest.raises(IntegrityError):
                mutation_session.execute(statement)
                mutation_session.commit()
            mutation_session.rollback()

        # This transition is valid in isolation, but omits the matching summary
        # update. The deferred cross-row constraint rejects the stale audit view
        # at commit rather than permitting contradictory evidence.
        open_exception = next(
            item
            for item in items
            if item["result"] == "mismatched" and item["resolution_status"] == "open"
        )
        with pytest.raises(IntegrityError):
            mutation_session.execute(
                update(ReconciliationItem)
                .where(ReconciliationItem.id == UUID(open_exception["id"]))
                .values(
                    resolution_status="ignored",
                    resolution_note="Direct transition without summary",
                    resolved_at=datetime.now(UTC),
                )
            )
            mutation_session.commit()
        mutation_session.rollback()

        # An internally self-consistent summary still cannot omit an eligible
        # ledger transaction from the evidence set.
        ledger_only = next(item for item in items if item["result"] == "ledger_only")
        run = mutation_session.get(ReconciliationRun, RECONCILIATION_RUN_ID)
        assert run is not None and run.summary is not None
        incomplete_summary = {
            "counts": {
                **run.summary["counts"],
                "ledger_only": run.summary["counts"]["ledger_only"] - 1,
                "open_exceptions": run.summary["counts"]["open_exceptions"] - 1,
            },
            "gross_volume": dict(run.summary["gross_volume"]),
        }
        with pytest.raises(IntegrityError):
            mutation_session.execute(
                delete(ReconciliationItem).where(
                    ReconciliationItem.id == UUID(ledger_only["id"])
                )
            )
            mutation_session.execute(
                update(ReconciliationRun)
                .where(ReconciliationRun.id == RECONCILIATION_RUN_ID)
                .values(summary=incomplete_summary)
            )
            mutation_session.commit()
        mutation_session.rollback()

        event_id = mutation_session.scalar(
            select(OutboxEvent.id).order_by(OutboxEvent.id).limit(1)
        )
        assert event_id is not None
        with pytest.raises(IntegrityError):
            mutation_session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == event_id)
                .values(payload={"tampered": True})
            )
        mutation_session.rollback()

    db_session.expire_all()
    after = (
        db_session.scalar(select(func.count()).select_from(LedgerTransaction)),
        db_session.scalar(select(func.count()).select_from(LedgerEntry)),
    )
    assert after == before == (10, 20)


def test_outbox_identity_order_matches_commit_order() -> None:
    first_staged = Event()
    allow_first_commit = Event()
    second_staged = Event()

    def write_event(name: str, gate: Event | None = None) -> int:
        with SessionLocal() as session, session.begin():
            event = append_outbox_event(
                session,
                event_type="posting.created",
                aggregate_type="ledger_transaction",
                aggregate_id=uuid4(),
                payload={"operation": name},
            )
            session.flush()
            if name == "first":
                first_staged.set()
            else:
                second_staged.set()
            if gate is not None:
                assert gate.wait(timeout=3)
            return event.id

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(write_event, "first", allow_first_commit)
        assert first_staged.wait(timeout=3)
        second = executor.submit(write_event, "second")
        second_was_blocked = not second_staged.wait(timeout=0.2)
        allow_first_commit.set()
        first_id = first.result(timeout=3)
        second_id = second.result(timeout=3)

    assert second_was_blocked
    assert first_id < second_id


def test_manual_reconciliation_closes_both_sides_with_auditable_evidence(
    client: TestClient, db_session: Session
) -> None:
    source = _create_account(client, "Manual Match Source")
    destination = _create_account(client, "Manual Match Destination")
    funding = _deposit(client, str(source["id"]), "100.00", "manual-match-fund")
    posted = _transfer(
        client,
        str(source["id"]),
        str(destination["id"]),
        "30.00",
        "manual-match-transfer",
    )
    transaction_id = UUID(str(posted["transaction_id"]))
    transaction = db_session.get(LedgerTransaction, transaction_id)
    assert transaction is not None

    run_id = uuid4()
    provider_item_id = uuid4()
    unknown_claim_id = uuid4()
    period_start = transaction.created_at - timedelta(seconds=1)
    period_end = transaction.created_at + timedelta(seconds=1)
    evidence_created_at = transaction.created_at + timedelta(seconds=2)
    db_session.add_all(
        [
            ReconciliationRun(
                id=run_id,
                provider="GulfPay Sandbox",
                fixture_key=f"manual-match-{run_id}",
                currency="AED",
                period_start=period_start,
                period_end=period_end,
                status="pending",
                summary=null(),
                created_at=evidence_created_at,
                completed_at=None,
            ),
            ReconciliationItem(
                id=provider_item_id,
                run_id=run_id,
                provider_reference="GULFPAY-MANUAL-MATCH-0001",
                claimed_transaction_id=unknown_claim_id,
                matched_transaction_id=None,
                amount=Decimal("30.00"),
                currency="AED",
                occurred_at=transaction.created_at,
                result="pending",
                mismatch_code=None,
                resolution_status="open",
                resolution_note=None,
                created_at=evidence_created_at,
                resolved_at=None,
            ),
        ]
    )
    db_session.commit()

    executed = client.post(f"/api/v1/reconciliation/runs/{run_id}/execute")
    assert executed.status_code == 200, executed.text
    assert executed.json()["summary"] == {
        "counts": {
            "matched": 0,
            "provider_only": 1,
            "ledger_only": 1,
            "mismatched": 0,
            "duplicate": 0,
            "open_exceptions": 2,
        },
        "gross_volume": {
            "currency": "AED",
            "provider_total": "30.00",
            "ledger_total": "30.00",
            "difference": "0.00",
        },
    }
    first_page = client.get(
        f"/api/v1/reconciliation/runs/{run_id}/items", params={"limit": 1}
    )
    assert first_page.status_code == 200, first_page.text
    cursor = first_page.json()["next_cursor"]
    assert cursor
    second_page = client.get(
        f"/api/v1/reconciliation/runs/{run_id}/items",
        params={"limit": 1, "cursor": cursor},
    )
    assert second_page.status_code == 200, second_page.text
    items = first_page.json()["items"] + second_page.json()["items"]
    provider_only = next(item for item in items if item["result"] == "provider_only")
    ledger_only = next(item for item in items if item["result"] == "ledger_only")

    cursor_mismatch = client.get(
        f"/api/v1/reconciliation/runs/{run_id}/items",
        params={"limit": 1, "cursor": cursor, "result": "ledger_only"},
    )
    assert cursor_mismatch.status_code == 422
    assert cursor_mismatch.json()["code"] == "cursor_filter_mismatch"

    forbidden = client.post(
        f"/api/v1/reconciliation/items/{ledger_only['id']}/match",
        json={"transaction_id": str(transaction_id)},
    )
    assert forbidden.status_code == 409
    assert forbidden.json()["code"] == "ledger_only_match_forbidden"

    missing_transaction = client.post(
        f"/api/v1/reconciliation/items/{provider_only['id']}/match",
        json={"transaction_id": str(uuid4())},
    )
    assert missing_transaction.status_code == 404
    assert missing_transaction.json()["code"] == "transaction_not_found"

    deposit_is_not_reconcilable = client.post(
        f"/api/v1/reconciliation/items/{provider_only['id']}/match",
        json={"transaction_id": str(funding["transaction_id"])},
    )
    assert deposit_is_not_reconcilable.status_code == 409

    matched = client.post(
        f"/api/v1/reconciliation/items/{provider_only['id']}/match",
        headers={"X-Request-ID": "manual-match-request"},
        json={
            "transaction_id": str(transaction_id),
            "note": "  Provider reference corrected  ",
        },
    )
    assert matched.status_code == 200, matched.text
    assert matched.json()["result"] == "matched"
    assert matched.json()["resolution_status"] == "matched"
    assert matched.json()["matched_transaction_id"] == str(transaction_id)
    assert matched.json()["resolution_note"] == "Provider reference corrected"
    matched_replay = client.post(
        f"/api/v1/reconciliation/items/{provider_only['id']}/match",
        json={
            "transaction_id": str(transaction_id),
            "note": "Provider reference corrected",
        },
    )
    assert matched_replay.status_code == 200, matched_replay.text
    assert matched_replay.json() == matched.json()

    resolved = client.get(
        f"/api/v1/reconciliation/runs/{run_id}/items",
        params={"resolution_status": "matched", "limit": 100},
    )
    assert resolved.status_code == 200, resolved.text
    assert {item["id"] for item in resolved.json()["items"]} == {
        provider_only["id"],
        ledger_only["id"],
    }
    assert (
        client.get(f"/api/v1/reconciliation/runs/{run_id}").json()["summary"]["counts"][
            "open_exceptions"
        ]
        == 0
    )

    already_resolved = client.post(
        f"/api/v1/reconciliation/items/{provider_only['id']}/match",
        json={"transaction_id": str(transaction_id)},
    )
    assert already_resolved.status_code == 409
    assert already_resolved.json()["code"] == "reconciliation_item_resolved"
    ignored_after_match = client.post(
        f"/api/v1/reconciliation/items/{provider_only['id']}/ignore",
        json={"reason": "Should remain immutable"},
    )
    assert ignored_after_match.status_code == 409

    db_session.expire_all()
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.event_type == "reconciliation.resolved")
        )
        == 2
    )


def test_event_stream_rejects_invalid_resume_cursor_as_a_problem(
    client: TestClient,
) -> None:
    response = client.get(
        "/api/v1/events/stream",
        headers={"Last-Event-ID": "01", "X-Request-ID": "invalid-stream-cursor"},
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "invalid_event_cursor"
    assert response.json()["request_id"] == "invalid-stream-cursor"
