"""Roles are resolved on the server: every route must refuse what the matrix refuses."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from conftest import API_KEY_HEADERS, as_user
from control_plane import rbac, web_api

ROOT_ID = 100001
STAFF = 400001
OPERATOR = 400002
FINANCE = 400003
SUPPORT = 400004
VIEWER = 400005
CHILD_ID = 400006


class FakePanel:
    def __init__(self, recorded):
        self.recorded = recorded

    def __call__(self, *args, **kwargs):
        return self

    def nodes(self):
        return {"nodes": [{"id": 1, "name": "Frankfurt-A", "enable": True}]}

    def set_node_enabled(self, node_id, enabled):
        self.recorded.append(("node", node_id, enabled))
        return {"ok": True}

    def users(self, admin_id=None, offset=0, limit=100):
        return {"users": [{"id": 9, "username": "sub-a", "status": True}], "total": 1}

    def set_user_enabled(self, user_id, enabled):
        self.recorded.append(("user", user_id, enabled))
        return {"ok": True}

    def set_admin_limit(self, admin_id, limit_use_in_bytes):
        self.recorded.append(("limit", admin_id, limit_use_in_bytes))
        return {"ok": True}


@pytest.fixture
def workspace(client, session, monkeypatch):
    """Root workspace with one registered, bound server and five staff actors."""
    calls = []
    monkeypatch.setattr(web_api, "Client", FakePanel(calls))
    boot = client.post("/v1/onboarding/bootstrap", headers=as_user(ROOT_ID),
                       json={"business_name": "Central Holdings", "slug": "central-holdings",
                             "setup_token": os.environ["INITIAL_SETUP_TOKEN"]})
    assert boot.status_code == 200, boot.text
    org_id = boot.json()["organization_id"]
    for telegram_id, role in ((STAFF, "reseller_admin"), (OPERATOR, "operator"), (FINANCE, "finance"),
                              (SUPPORT, "support"), (VIEWER, "viewer")):
        bound = client.post("/v1/admin/actor-bindings", headers=API_KEY_HEADERS,
                            json={"telegram_id": telegram_id, "organization_id": org_id,
                                  "display_name": role, "role": role})
        assert bound.status_code == 200, bound.text
    panel = client.post("/v1/webapp/panels", headers=as_user(ROOT_ID),
                        json={"name": "Frankfurt-01", "base_url": "https://panel.example.test",
                              "api_key": "role-matrix-key", "pg_admin_id": 42})
    assert panel.status_code == 200, panel.text
    return {"org_id": org_id, "panel_id": panel.json()["id"], "calls": calls}


def _role_of(client, telegram_id):
    return client.get("/v1/webapp/capabilities", headers=as_user(telegram_id)).json()


def test_matrix_is_closed_and_root_only_controls_are_not_delegated():
    for role, granted in rbac.MATRIX.items():
        assert granted <= set(rbac.ALL_PERMISSIONS), role
        assert granted, role
    for role in rbac.ROLES[1:]:
        assert "decide_adjustment" not in rbac.MATRIX[role], role
        assert "change_billing_coefficient" not in rbac.MATRIX[role], role
    assert rbac.permissions_for("wizard-of-os") == rbac.permissions_for("viewer")
    assert rbac.permissions_for(None) == rbac.permissions_for("viewer")
    assert rbac.MATRIX["viewer"] < rbac.MATRIX["reseller_admin"] < rbac.MATRIX["system_admin"]


def test_capabilities_come_from_the_matrix_and_never_from_the_browser(client, workspace):
    for telegram_id, expected in ((ROOT_ID, "system_admin"), (OPERATOR, "operator"), (VIEWER, "viewer")):
        caps = _role_of(client, telegram_id)
        assert caps["role"] == expected, telegram_id
        assert caps["role_fa"] == rbac.role_fa(expected)
        assert caps["permissions"] == rbac.permission_payload(expected)
    assert _role_of(client, ROOT_ID)["is_system_admin"] is True
    assert _role_of(client, OPERATOR)["is_system_admin"] is False


def test_panel_writes_require_the_matching_role(client, workspace):
    panel = workspace["panel_id"]
    assert client.get(f"/v1/webapp/panels/{panel}/nodes", headers=as_user(OPERATOR)).status_code == 200
    assert client.post(f"/v1/webapp/panels/{panel}/nodes/1/disable", headers=as_user(OPERATOR)).status_code == 200
    assert client.post(f"/v1/webapp/panels/{panel}/nodes/1/enable", headers=as_user(VIEWER)).status_code == 403
    assert client.post(f"/v1/webapp/panels/{panel}/nodes/1/enable", headers=as_user(FINANCE)).status_code == 403
    assert client.post(f"/v1/webapp/panels/{panel}/limit", headers=as_user(SUPPORT),
                       json={"limit_use_in_bytes": 100}).status_code == 403
    assert workspace["calls"] == [("node", 1, False)], "a refusal must not reach the panel"


def test_subscriber_control_is_its_own_permission(client, workspace):
    panel = workspace["panel_id"]
    assert client.get(f"/v1/webapp/panels/{panel}/users", headers=as_user(SUPPORT)).status_code == 200
    assert client.post(f"/v1/webapp/panels/{panel}/users/9/disable", headers=as_user(SUPPORT)).status_code == 200
    assert client.get(f"/v1/webapp/panels/{panel}/users", headers=as_user(VIEWER)).status_code == 403
    assert client.post(f"/v1/webapp/panels/{panel}/users/9/reset-data", headers=as_user(VIEWER)).status_code == 403


def test_pricing_stays_with_the_system_administrator(client, session, workspace):
    panel = workspace["panel_id"]
    refused = client.post(f"/v1/webapp/panels/{panel}/coefficient", headers=as_user(STAFF), json={"usage_coefficient": "2"})
    assert refused.status_code == 403
    assert client.post(f"/v1/webapp/panels/{panel}/coefficient", headers=as_user(ROOT_ID), json={"usage_coefficient": "2"}).status_code == 200
    detail = client.post(f"/v1/webapp/panels/{panel}/coefficient", headers=as_user(STAFF), json={"usage_coefficient": "2"}).json()["detail"]
    assert "تغییر ضریب" in detail


def test_only_network_managers_can_open_a_reseller(client, workspace):
    for n, telegram_id in enumerate((OPERATOR, VIEWER, SUPPORT)):
        refused = client.post("/v1/webapp/children", headers=as_user(telegram_id),
                              json={"name": f"Branch {n}", "slug": f"branch-{n}", "price_per_gib_irr": 1000})
        assert refused.status_code == 403, telegram_id
    assert client.post("/v1/webapp/children", headers=as_user(STAFF),
                       json={"name": "Branch", "slug": "branch-staff", "price_per_gib_irr": 1000}).status_code == 200


def test_a_direct_parent_releases_credit_for_its_own_branch(client, session, workspace):
    child = client.post("/v1/webapp/children", headers=as_user(STAFF),
                        json={"name": "North Office", "slug": "north-office", "price_per_gib_irr": 1500, "credit_limit_irr": 50_000}).json()
    client.post("/v1/admin/actor-bindings", headers=API_KEY_HEADERS,
                json={"telegram_id": CHILD_ID, "organization_id": child["id"], "display_name": "North", "role": "reseller_admin"})
    request_id = client.post("/v1/webapp/funding-requests", headers=as_user(CHILD_ID),
                             json={"amount_irr": 25_000}).json()["id"]

    listed = client.get("/v1/webapp/funding-requests", headers=as_user(STAFF)).json()
    assert [row["id"] for row in listed] == [request_id]
    # A staff member without the money permission cannot even read the queue.
    assert client.get("/v1/webapp/funding-requests", headers=as_user(OPERATOR)).status_code == 403
    assert client.post(f"/v1/webapp/funding-requests/{request_id}/approve", headers=as_user(OPERATOR)).status_code == 403
    decided = client.post(f"/v1/webapp/funding-requests/{request_id}/approve", headers=as_user(STAFF))
    assert decided.status_code == 200 and decided.json()["status"] == "approved"
    wallet = int(client.get("/v1/webapp/dashboard", headers=as_user(CHILD_ID)).json()["wallet"]["balance_irr"])
    assert wallet == 25_000
    assert session.scalar(text("SELECT count(*) FROM audit_logs WHERE action='funding_request.approve'")) == 1
    assert session.scalar(text("SELECT count(*) FROM ledger_imbalance")) == 0


def test_viewer_reads_and_cannot_act(client, workspace):
    assert client.get("/v1/webapp/dashboard", headers=as_user(VIEWER)).status_code == 200
    assert client.get("/v1/webapp/transactions", headers=as_user(VIEWER)).status_code == 200
    assert client.post("/v1/webapp/funding-requests", headers=as_user(VIEWER), json={"amount_irr": 5000}).status_code == 403
    assert client.post("/v1/webapp/panels", headers=as_user(VIEWER),
                       json={"name": "Nope", "base_url": "https://nope.example.test", "api_key": "x" * 12}).status_code == 403


def test_audit_rows_are_named_and_limited_to_the_callers_subtree(client, session, workspace):
    assert client.post(f"/v1/webapp/panels/{workspace['panel_id']}/limit", headers=as_user(OPERATOR),
                       json={"limit_use_in_bytes": 100}).status_code == 200
    rows = client.get("/v1/webapp/audit?limit=50", headers=as_user(ROOT_ID)).json()
    assert client.get("/v1/webapp/audit?limit=0", headers=as_user(ROOT_ID)).status_code == 422
    assert client.get("/v1/webapp/audit", headers=as_user(VIEWER)).status_code == 403
    bootstrap = next(row for row in rows if row["action"] == "workspace.bootstrap")
    assert bootstrap["organization"] == "Central Holdings" and bootstrap["actor"] == "Test"
    limit_row = next(row for row in rows if row["action"] == "admin.limit")
    assert limit_row["actor"] == "operator", "an operator action must be attributed to the operator"
    child = client.post("/v1/webapp/children", headers=as_user(STAFF),
                        json={"name": "Branch Audit", "slug": "branch-audit", "price_per_gib_irr": 1000}).json()
    client.post("/v1/admin/actor-bindings", headers=API_KEY_HEADERS,
                json={"telegram_id": CHILD_ID, "organization_id": child["id"], "display_name": "Branch", "role": "reseller_admin"})
    assert [row["target_id"] for row in
            client.get("/v1/webapp/audit?limit=200", headers=as_user(STAFF)).json()
            if row["action"] == "organization.create"] == [child["id"]]
    scoped = client.get("/v1/webapp/audit?limit=200", headers=as_user(CHILD_ID)).json()
    assert scoped and {row["organization_id"] for row in scoped} == {child["id"]}
    assert "workspace.bootstrap" not in [row["action"] for row in scoped]


def test_bindings_reject_a_role_that_does_not_exist(client, session, workspace):
    refused = client.post("/v1/admin/actor-bindings", headers=API_KEY_HEADERS,
                          json={"telegram_id": 555001, "organization_id": workspace["org_id"],
                                "display_name": "Ghost", "role": "superuser"})
    assert refused.status_code == 422
    assert session.scalar(text("SELECT count(*) FROM actors WHERE telegram_id=555001")) == 0
