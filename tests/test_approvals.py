"""Sensitive money operations require a separate approval decision before they move funds."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import func, select, text

from conftest import as_user

ROOT_ID = 100001
STAFF_ID = 200003
OTHER_ID = 300004
NOTE = "corrections after a duplicated settlement"


@pytest.fixture
def staff(client, session):
    client.post(
        "/v1/onboarding/bootstrap",
        headers=as_user(ROOT_ID),
        json={"business_name": "Central Holdings", "slug": "central-holdings", "setup_token": os.environ["INITIAL_SETUP_TOKEN"]},
    )
    created = client.post(
        "/v1/webapp/admin/resellers",
        headers=as_user(ROOT_ID),
        json={"name": "Staff Office", "slug": "staff", "telegram_id": STAFF_ID, "price_per_gib_irr": 2000, "credit_limit_irr": 100_000},
    )
    assert created.status_code == 200, created.text
    return created.json()["id"]


def _wallet(client, organization_id, headers=None):
    return int(client.get(f"/v1/organizations/{organization_id}/wallet", headers=headers or as_user(ROOT_ID)).json()["balance_irr"])


def _request(client, organization_id, amount, note=NOTE, headers=None):
    return client.post(
        "/v1/webapp/admin/adjustments",
        headers=headers or as_user(STAFF_ID),
        json={"organization_id": organization_id, "amount_irr": amount, "note": note},
    )


def test_adjustment_waits_for_approval_and_moves_no_money(client, session, staff):
    response = _request(client, staff, 90_000)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert "id" in body
    assert _wallet(client, staff) == 0
    assert session.scalar(select(func.count()).select_from(text("transactions"))) == 0
    actions = set(session.scalars(text("SELECT action FROM audit_logs")))
    assert "adjustment.request" in actions and "adjustment.apply" not in actions


def test_approve_applies_exactly_once_and_is_audited(client, session, staff):
    approval_id = _request(client, staff, 90_000).json()["id"]
    decided = client.post(f"/v1/webapp/admin/approvals/{approval_id}/approve", headers=as_user(ROOT_ID))
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "executed"
    assert _wallet(client, staff) == 90_000
    assert session.scalar(text("SELECT approver_id FROM approval_requests WHERE id=:i"), {"i": approval_id}) is not None
    assert session.scalar(text("SELECT count(*) FROM ledger_imbalance")) == 0
    assert set(session.scalars(text("SELECT action FROM audit_logs"))) >= {"adjustment.request", "approval.approve", "adjustment.apply"}
    again = client.post(f"/v1/webapp/admin/approvals/{approval_id}/approve", headers=as_user(ROOT_ID))
    assert again.status_code == 409
    assert _wallet(client, staff) == 90_000


def test_reject_leaves_the_wallet_untouched(client, session, staff):
    approval_id = _request(client, staff, 50_000).json()["id"]
    decided = client.post(f"/v1/webapp/admin/approvals/{approval_id}/reject", headers=as_user(ROOT_ID))
    assert decided.json()["status"] == "rejected"
    assert _wallet(client, staff) == 0
    assert session.scalar(select(func.count()).select_from(text("transactions"))) == 0


def test_only_the_system_administrator_decides(client, staff):
    approval_id = _request(client, staff, 10_000).json()["id"]
    assert client.post(f"/v1/webapp/admin/approvals/{approval_id}/approve", headers=as_user(STAFF_ID)).status_code == 403
    assert client.post(f"/v1/webapp/admin/approvals/{approval_id}/approve", headers=as_user(OTHER_ID)).status_code == 403


def test_debit_adjustment_respects_the_credit_limit(client, session, staff):
    credit_id = _request(client, staff, 90_000).json()["id"]
    assert client.post(f"/v1/webapp/admin/approvals/{credit_id}/approve", headers=as_user(ROOT_ID)).status_code == 200
    assert _wallet(client, staff) == 90_000
    approval_id = _request(client, staff, -200_000, note="recover an over-credit").json()["id"]
    decided = client.post(f"/v1/webapp/admin/approvals/{approval_id}/approve", headers=as_user(ROOT_ID))
    assert decided.status_code == 409
    assert "insufficient credit" in decided.json()["detail"]
    assert session.scalar(select(func.count()).select_from(text("transactions"))) == 1
    assert _wallet(client, staff) == 90_000


def test_expired_requests_cannot_be_executed(client, session, staff):
    approval_id = _request(client, staff, 30_000).json()["id"]
    session.execute(text("UPDATE approval_requests SET expires_at=now()-interval '1 hour' WHERE id=:i"), {"i": approval_id})
    session.commit()
    expired = client.post(f"/v1/webapp/admin/approvals/{approval_id}/approve", headers=as_user(ROOT_ID))
    assert expired.status_code == 410
    assert session.scalar(text("SELECT status FROM approval_requests WHERE id=:i"), {"i": approval_id}) == "expired"
    assert _wallet(client, staff) == 0


def test_grant_is_notified_through_the_outbox(client, session, staff):
    from control_plane import outbox

    approval_id = _request(client, staff, 30_000).json()["id"]
    client.post(f"/v1/webapp/admin/approvals/{approval_id}/approve", headers=as_user(ROOT_ID))
    recorder_messages = []

    def notify(message, chat_ids):
        recorder_messages.append((message, list(chat_ids)))
        return 1

    stats = outbox.run_once(handlers={"approval.granted": lambda s, e: outbox.handle_approval_granted(s, e, notifier=notify)})
    assert stats["done"] == 1
    assert "executed" in recorder_messages[0][0]


def test_root_may_apply_directly_but_still_leaves_an_audit_trail(client, session, staff):
    response = _request(client, staff, 5_000, note="manual correction by the system administrator", headers=as_user(ROOT_ID))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "executed"
    assert _wallet(client, staff) == 5_000
    actions = list(session.scalars(text("SELECT action FROM audit_logs")))
    assert actions.count("adjustment.apply") == 1
