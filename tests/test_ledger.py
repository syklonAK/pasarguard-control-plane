"""Money invariants: double-entry, idempotency, cached balance guard and settlement cascade."""
from __future__ import annotations

import pytest
from sqlalchemy import func, select, text


def _balance(session, org_id):
    from control_plane.app import Account

    return int(session.scalar(select(Account.balance_irr).where(Account.owner_key == org_id)))


def _entry_totals(session, account_id):
    return session.execute(
        text("SELECT COALESCE(SUM(CASE WHEN side='credit' THEN amount_irr ELSE -amount_irr END),0) FROM entries WHERE account_id=:a"),
        {"a": account_id},
    ).scalar()


def test_transfer_writes_balanced_double_entry(session, make_org):
    from control_plane.app import Account, Entry, Transaction, transfer

    parent, child = make_org(), make_org(make_org())
    transfer(session, "SYSTEM", child.id, 500_000, "fund-1", "fund", "seed", enforce=False)
    transfer(session, child.id, parent.id, 120_000, "usage-1", "usage", parent.id)
    session.commit()
    tx = session.scalar(select(Transaction).where(Transaction.idempotency_key == "usage-1"))
    entries = session.scalars(select(Entry).where(Entry.transaction_id == tx.id)).all()
    assert {(e.side, e.amount_irr) for e in entries} == {("debit", 120_000), ("credit", 120_000)}
    assert _balance(session, child.id) == 380_000
    assert _balance(session, parent.id) == 120_000
    assert int(session.scalar(select(Account.balance_irr).where(Account.owner_key == "SYSTEM"))) == -500_000
    assert session.execute(text("SELECT count(*) FROM ledger_imbalance")).scalar() == 0


def test_idempotency_key_replays_without_duplicate_entries(session, make_org):
    from control_plane.app import Entry, Transaction, transfer

    org = make_org()
    first = transfer(session, "SYSTEM", org.id, 10_000, "dup-key", "fund", "x", enforce=False)
    session.commit()
    again = transfer(session, "SYSTEM", org.id, 99_999, "dup-key", "fund", "x", enforce=False)
    session.commit()
    assert again.id == first.id
    assert _balance(session, org.id) == 10_000
    assert session.scalar(select(func.count()).select_from(Entry)) == 2
    assert session.scalar(select(func.count()).select_from(Transaction)) == 1


@pytest.mark.parametrize("amount", [0, -5])
def test_non_positive_amounts_are_rejected(session, make_org, amount):
    from control_plane.app import transfer

    org = make_org()
    with pytest.raises(ValueError, match="amount must be positive"):
        transfer(session, "SYSTEM", org.id, amount, "neg-key", "fund", "x", enforce=False)


def test_credit_limit_is_enforced_and_balance_untouched(session, make_org):
    from control_plane.app import transfer

    child = make_org(credit_limit_irr=50_000)
    transfer(session, "SYSTEM", child.id, 10_000, "fund-seed", "fund", "seed", enforce=False)
    session.commit()
    with pytest.raises(ValueError, match="insufficient credit"):
        transfer(session, child.id, "SYSTEM", 60_001, "over-limit", "usage", "r")
    session.rollback()
    assert _balance(session, child.id) == 10_000


def test_balance_cannot_be_edited_without_ledger_write(session, make_org):
    org = make_org()
    with pytest.raises(Exception, match="only change through a ledger transaction"):
        session.execute(text("UPDATE accounts SET balance_irr = 999 WHERE owner_key=:o"), {"o": org.id})
        session.commit()


def test_suspended_organization_cannot_be_billed(session, make_org):
    from control_plane.app import Organization, transfer

    org = make_org(credit_limit_irr=1_000_000)
    session.execute(text("UPDATE organizations SET status='suspended' WHERE id=:id"), {"id": org.id})
    session.commit()
    with pytest.raises(ValueError, match="organization suspended"):
        transfer(session, org.id, "SYSTEM", 10, "suspended-pay", "usage", "r")


def test_settlement_cascades_profit_per_level(session, make_org, make_binding):
    from control_plane.domain import GIB

    from control_plane.app import Organization, UsageIn, observe

    root = make_org()
    middle = make_org(root, price_per_gib_irr=3000, credit_limit_irr=100_000)
    leaf = make_org(middle, price_per_gib_irr=5000, credit_limit_irr=100_000)
    binding = make_binding(leaf)
    observe(session, UsageIn(binding_id=binding.id, lifetime_bytes=0, observed_at=_ts()))
    session.commit()
    rows = observe(session, UsageIn(binding_id=binding.id, lifetime_bytes=GIB, observed_at=_ts()))
    session.commit()
    assert [r["amount_irr"] for r in rows] == [5000, 3000]
    assert [r["child_id"] for r in rows] == [leaf.id, middle.id]
    assert _balance(session, leaf.id) == -5000
    assert _balance(session, middle.id) == 2000
    assert _balance(session, root.id) == 3000
    assert session.execute(text("SELECT count(*) FROM ledger_imbalance")).scalar() == 0


def test_usage_coefficient_scales_billable_traffic(session, make_org, make_binding):
    from control_plane.domain import GIB

    from control_plane.app import UsageIn, observe

    root = make_org()
    leaf = make_org(root, price_per_gib_irr=1000, credit_limit_irr=100_000)
    binding = make_binding(leaf)
    session.execute(text("UPDATE panels SET usage_coefficient=1.25 WHERE id=:p"), {"p": binding.panel_id})
    session.commit()
    observe(session, UsageIn(binding_id=binding.id, lifetime_bytes=0, observed_at=_ts()))
    session.commit()
    rows = observe(session, UsageIn(binding_id=binding.id, lifetime_bytes=GIB, observed_at=_ts()))
    session.commit()
    assert [r["amount_irr"] for r in rows] == [1250]


def test_regression_is_not_billed_and_confirmed_reset_rebaselines(session, make_org, make_binding):
    from control_plane.domain import GIB

    from control_plane.app import Checkpoint, Usage, UsageIn, observe

    root = make_org()
    leaf = make_org(root, price_per_gib_irr=1000, credit_limit_irr=100_000)
    binding = make_binding(leaf)
    observe(session, UsageIn(binding_id=binding.id, lifetime_bytes=0, observed_at=_ts()))
    session.commit()
    observe(session, UsageIn(binding_id=binding.id, lifetime_bytes=5 * GIB, observed_at=_ts()))
    session.commit()
    assert _balance(session, root.id) == 5000
    for _ in range(2):
        observe(session, UsageIn(binding_id=binding.id, lifetime_bytes=1 * GIB, observed_at=_ts()))
    session.commit()
    assert session.scalar(select(func.count()).select_from(Usage).where(Usage.status == "regression")) == 2
    assert _balance(session, root.id) == 5000
    observe(session, UsageIn(binding_id=binding.id, lifetime_bytes=1 * GIB, observed_at=_ts()))
    session.commit()
    assert session.scalar(select(func.count()).select_from(Usage).where(Usage.status == "reset_baseline")) == 1
    assert session.scalar(select(Checkpoint).where(Checkpoint.binding_id == binding.id)).lifetime_bytes == GIB
    observe(session, UsageIn(binding_id=binding.id, lifetime_bytes=2 * GIB, observed_at=_ts()))
    session.commit()
    assert _balance(session, root.id) == 6000


def test_duplicate_observation_is_not_billed_twice(session, make_org, make_binding):
    from control_plane.domain import GIB

    from control_plane.app import UsageIn, observe

    root = make_org()
    leaf = make_org(root, price_per_gib_irr=1000, credit_limit_irr=100_000)
    binding = make_binding(leaf)
    observe(session, UsageIn(binding_id=binding.id, lifetime_bytes=0, observed_at=_ts()))
    session.commit()
    observe(session, UsageIn(binding_id=binding.id, lifetime_bytes=GIB, observed_at=_ts()))
    session.commit()
    assert _balance(session, root.id) == 1000
    observe(session, UsageIn(binding_id=binding.id, lifetime_bytes=GIB, observed_at=_ts()))
    session.commit()
    assert _balance(session, root.id) == 1000


def test_concurrent_settlement_cannot_double_spend(session, make_org):
    import threading

    from control_plane.app import SessionLocal, transfer

    org = make_org(credit_limit_irr=0)
    transfer(session, "SYSTEM", org.id, 100_000, "race-fund", "fund", "seed", enforce=False)
    session.commit()

    errors, done = [], []

    def spend(label):
        with SessionLocal() as worker:
            try:
                transfer(worker, org.id, "SYSTEM", 60_000, f"race-{label}", "usage", "r")
                worker.commit()
                done.append(label)
            except Exception as exc:  # deadlock/serialization is expected and retried below
                worker.rollback()
                errors.append(str(exc))

    threads = [threading.Thread(target=spend, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(done) == 1, (done, errors, _balance(session, org.id))
    assert _balance(session, org.id) == 40_000
    assert session.execute(text("SELECT count(*) FROM ledger_imbalance")).scalar() == 0


def test_depth_limit_blocks_deeper_trees(session, make_org, client):
    from control_plane.app import Organization

    root = make_org()
    session.execute(text("UPDATE organizations SET max_depth=2 WHERE id=:id"), {"id": root.id})
    session.commit()
    level1 = make_org(root)
    level2 = make_org(level1)
    response = client.post(
        "/v1/organizations",
        json={"name": "Too Deep", "slug": "too-deep", "parent_id": level2.id, "price_per_gib_irr": 10},
    )
    assert response.status_code == 409
    assert "maximum reseller depth" in response.json()["detail"]
    assert session.scalar(select(Organization.id).where(Organization.slug == "too-deep")) is None


def _ts():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
