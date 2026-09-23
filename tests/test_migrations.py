"""Proves the PostgreSQL-backed harness: migrations on an empty database and on an existing one."""
from __future__ import annotations

import pytest
from sqlalchemy import text

EXPECTED_TABLES = {
    "organizations",
    "organization_closure",
    "contracts",
    "accounts",
    "transactions",
    "entries",
    "panels",
    "panel_owners",
    "bindings",
    "usage_checkpoints",
    "usage_observations",
    "funding_requests",
    "outbox",
    "actors",
    "memberships",
    "system_settings",
    "schema_migrations",
    "audit_logs",
    "approval_requests",
}


def _tables(session) -> set:
    return set(
        session.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        ).scalars()
    )


def test_migrations_create_expected_schema(session, engine):
    assert EXPECTED_TABLES <= _tables(session)


def test_migrations_are_idempotent(engine):
    from control_plane.migrate import run_migrations

    with engine.connect() as probe:
        before = _tables(probe)
    run_migrations(engine)
    run_migrations(engine)
    with engine.connect() as probe:
        applied = probe.execute(text("SELECT count(*) FROM schema_migrations")).scalar()
        assert _tables(probe) == before
    assert applied == 3


def test_ledger_is_append_only(session, make_org):
    from sqlalchemy.exc import DBAPIError

    make_org()
    tx_id = "00000000-0000-0000-0000-0000000000aa"
    session.execute(
        text("INSERT INTO transactions(id,idempotency_key,kind,reference,created_at) VALUES (:id,'ledger-probe','fund','probe',now())"),
        {"id": tx_id},
    )
    session.execute(
        text("INSERT INTO entries(id,transaction_id,account_id,side,amount_irr) VALUES (:id,:tx,:account,'debit',10)"),
        {"id": "00000000-0000-0000-0000-0000000000bb", "tx": tx_id, "account": session.scalar(text("SELECT id FROM accounts LIMIT 1"))},
    )
    session.commit()
    with pytest.raises(DBAPIError, match="ledger is append-only"):
        session.execute(text("UPDATE transactions SET kind='tampered' WHERE id=:id"), {"id": tx_id})
