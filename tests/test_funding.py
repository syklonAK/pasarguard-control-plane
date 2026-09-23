"""Funding requests: only the root administrator can decide, and approving credits exactly once."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import func, select, text

from conftest import as_user

ROOT_ID = 100001
RESELLER_ID = 200002


@pytest.fixture
def workspace(client, session):
    response = client.post(
        "/v1/onboarding/bootstrap",
        headers=as_user(ROOT_ID),
        json={"business_name": "Central Holdings", "slug": "central-holdings", "setup_token": os.environ["INITIAL_SETUP_TOKEN"]},
    )
    assert response.status_code == 200, response.text
    return response.json()["organization_id"]


def _create_reseller(client, telegram_id: int, slug: str) -> str:
    response = client.post(
        "/v1/webapp/admin/resellers",
        headers=as_user(ROOT_ID),
        json={"name": f"Reseller {slug}", "slug": slug, "telegram_id": telegram_id, "price_per_gib_irr": 2000, "credit_limit_irr": 0},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _wallet(client, headers, organization_id) -> int:
    return int(client.get(f"/v1/organizations/{organization_id}/wallet", headers=headers).json()["balance_irr"])


def test_approve_credits_wallet_once_and_rejects_second_decision(client, session, workspace):
    child_id = _create_reseller(client, RESELLER_ID, "north")
    created = client.post("/v1/webapp/funding-requests", headers=as_user(RESELLER_ID), json={"amount_irr": 750_000})
    assert created.status_code == 200, created.text
    request_id = created.json()["id"]

    approved = client.post(f"/v1/webapp/admin/funding-requests/{request_id}/approve", headers=as_user(ROOT_ID))
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert _wallet(client, as_user(ROOT_ID), child_id) == 750_000

    again = client.post(f"/v1/webapp/admin/funding-requests/{request_id}/approve", headers=as_user(ROOT_ID))
    assert again.status_code == 409
    assert _wallet(client, as_user(ROOT_ID), child_id) == 750_000
    assert session.execute(text("SELECT count(*) FROM ledger_imbalance")).scalar() == 0
    funds = session.scalar(select(func.count()).select_from(text("transactions")).where(text("kind='fund'")))
    assert funds == 1


def test_reject_leaves_balance_unchanged(client, session, workspace):
    child_id = _create_reseller(client, RESELLER_ID, "south")
    created = client.post("/v1/webapp/funding-requests", headers=as_user(RESELLER_ID), json={"amount_irr": 400_000})
    request_id = created.json()["id"]
    decided = client.post(f"/v1/webapp/admin/funding-requests/{request_id}/reject", headers=as_user(ROOT_ID))
    assert decided.json()["status"] == "rejected"
    assert _wallet(client, as_user(ROOT_ID), child_id) == 0
    assert session.scalar(select(func.count()).select_from(text("transactions"))) == 0


def test_only_root_can_decide_and_requests_come_from_membership(client, session, workspace):
    child_id = _create_reseller(client, RESELLER_ID, "west")
    created = client.post("/v1/webapp/funding-requests", headers=as_user(RESELLER_ID), json={"amount_irr": 10_000})
    request_id = created.json()["id"]
    forbidden = client.post(f"/v1/webapp/admin/funding-requests/{request_id}/approve", headers=as_user(RESELLER_ID))
    assert forbidden.status_code == 403
    assert _wallet(client, as_user(ROOT_ID), child_id) == 0


def test_funding_decisions_are_audited(client, session, workspace):
    child_id = _create_reseller(client, RESELLER_ID, "east")
    created = client.post("/v1/webapp/funding-requests", headers=as_user(RESELLER_ID), json={"amount_irr": 250_000})
    client.post(f"/v1/webapp/admin/funding-requests/{created.json()['id']}/approve", headers=as_user(ROOT_ID))
    session.expire_all()
    actions = set(session.scalars(text("SELECT action FROM audit_logs")))
    assert {"reseller.create", "funding_request.create", "funding_request.approve"} <= actions
    assert session.scalar(text("SELECT actor_id FROM audit_logs WHERE action='funding_request.approve'")) is not None
    assert _wallet(client, as_user(ROOT_ID), child_id) == 250_000
