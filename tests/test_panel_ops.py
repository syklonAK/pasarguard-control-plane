"""Panel operations must be audited, permission-checked, and free of upstream leakage."""
from __future__ import annotations

import json
import os

import httpx
import pytest
from sqlalchemy import select, text

from conftest import as_user
from control_plane import v04_app
from control_plane.app import Panel

ROOT_ID = 100001
STAFF_ID = 200003
API_KEY = "panel-operation-key"
OWNER_PASSWORD = "owner-operation-password"
URL = "https://panel.example.test"


class FakePanel:
    """Records the operations the control plane asks the panel to perform."""

    def __init__(self, recorded, failure=None):
        self.recorded = recorded
        self.failure = failure

    def __call__(self, base_url, api_key=None, owner_username=None, owner_password=None, verify_tls=True):
        self.recorded.append({"init": (base_url, api_key, owner_username, owner_password, verify_tls)})
        return self

    def _call(self, name, *args):
        self.recorded.append({"op": name, "args": args})
        if self.failure is not None:
            request = httpx.Request("POST", f"{URL}/api/endpoint-with-a-secret-token")
            raise httpx.HTTPStatusError(f"{URL} failed", request=request,
                                        response=httpx.Response(self.failure, request=request))
        return {"ok": True, "name": name}

    def nodes(self):
        return self._call("nodes")

    def node_status(self, node_id):
        return self._call("node_status", node_id)

    def set_node_enabled(self, node_id, enabled):
        return self._call("set_node_enabled", node_id, enabled)

    def reconnect_node(self, node_id):
        return self._call("reconnect_node", node_id)

    def users(self, admin_id, offset=0, limit=50):
        self._call("users", admin_id, offset, limit)
        return {"users": [{"id": 9, "status": "active"}], "total": 1}

    def set_user_enabled(self, user_id, enabled):
        return self._call("set_user_enabled", user_id, enabled)

    def reset_user_data(self, user_id):
        return self._call("reset_user_data", user_id)

    def set_admin_limit(self, admin_id, limit_use_in_bytes):
        return self._call("set_admin_limit", admin_id, limit_use_in_bytes)


@pytest.fixture
def fake_panel(monkeypatch):
    recorded = []
    monkeypatch.setattr(v04_app, "Client", FakePanel(recorded))
    return recorded


@pytest.fixture
def panel(client, session, fake_panel):
    client.post(
        "/v1/onboarding/bootstrap",
        headers=as_user(ROOT_ID),
        json={"business_name": "Central Holdings", "slug": "central-holdings", "setup_token": os.environ["INITIAL_SETUP_TOKEN"]},
    )
    created = client.post(
        "/v1/webapp/panels",
        headers=as_user(ROOT_ID),
        json={"name": "Frankfurt-01", "base_url": f"{URL}/", "api_key": API_KEY,
              "owner_username": "owner", "owner_password": OWNER_PASSWORD, "pg_admin_id": 42},
    )
    assert created.status_code == 200, created.text
    fake_panel.clear()
    return created.json()["id"]


def test_node_toggle_reaches_the_panel_and_is_audited(client, session, panel, fake_panel):
    response = client.post(f"/v1/webapp/panels/{panel}/nodes/5/enable", headers=as_user(ROOT_ID))
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is True
    ops = [c for c in fake_panel if "op" in c]
    assert ops[0] == {"op": "set_node_enabled", "args": (5, True)}
    assert fake_panel[0]["init"] == (URL, API_KEY, "owner", OWNER_PASSWORD, True)
    assert session.scalar(text("SELECT count(*) FROM audit_logs WHERE action='node.enable'")) == 1
    assert session.scalar(text("SELECT count(*) FROM audit_logs WHERE action='node.status.read'")) == 0


def test_node_status_and_listing_are_reads_not_mutations(client, session, panel, fake_panel):
    assert client.get(f"/v1/webapp/panels/{panel}/nodes", headers=as_user(ROOT_ID)).status_code == 200
    assert client.get(f"/v1/webapp/panels/{panel}/nodes/5/status", headers=as_user(ROOT_ID)).status_code == 200
    assert [c["op"] for c in fake_panel if "op" in c] == ["nodes", "node_status"]
    assert set(session.scalars(text("SELECT action FROM audit_logs WHERE action LIKE 'node.%' OR action LIKE 'panel.nodes%'"))) == {"panel.nodes.read", "node.status.read"}


def test_unknown_toggle_words_never_reach_the_panel(client, panel, fake_panel):
    assert client.post(f"/v1/webapp/panels/{panel}/nodes/5/banana", headers=as_user(ROOT_ID)).status_code == 422
    assert [c for c in fake_panel if "op" in c] == []


def test_user_reset_data_is_not_shadowed_by_the_toggle_route(client, session, panel, fake_panel):
    reset = client.post(f"/v1/webapp/panels/{panel}/users/9/reset-data", headers=as_user(ROOT_ID))
    assert reset.status_code == 200, reset.text
    assert client.post(f"/v1/webapp/panels/{panel}/users/9/disable", headers=as_user(ROOT_ID)).status_code == 200
    assert [c["op"] for c in fake_panel if "op" in c] == ["reset_user_data", "set_user_enabled"]
    assert set(session.scalars(text("SELECT action FROM audit_logs WHERE action LIKE 'user.%'"))) == {"user.reset_data", "user.disable"}


def test_user_listing_is_scoped_to_the_bound_panel_admin(client, session, panel, fake_panel):
    body = client.get(f"/v1/webapp/panels/{panel}/users?offset=20&limit=10", headers=as_user(ROOT_ID)).json()
    assert body["admin_id"] == 42 and body["users"] == [{"id": 9, "status": "active"}] and body["total"] == 1
    assert fake_panel[-1]["args"] == (42, 20, 10)
    assert client.get(f"/v1/webapp/panels/{panel}/users?limit=500", headers=as_user(ROOT_ID)).status_code == 422


def test_admin_limit_update_uses_the_binding_and_records_the_value(client, session, panel, fake_panel):
    response = client.post(f"/v1/webapp/panels/{panel}/limit", headers=as_user(ROOT_ID), json={"limit_use_in_bytes": 5368709120})
    assert response.status_code == 200, response.text
    assert fake_panel[-1] == {"op": "set_admin_limit", "args": (42, 5368709120)}
    assert session.scalar(text("SELECT metadata ->> 'limit_use_in_bytes' FROM audit_logs WHERE action='admin.limit'")) == "5368709120"
    assert client.post(f"/v1/webapp/panels/{panel}/limit", headers=as_user(ROOT_ID), json={"limit_use_in_bytes": -1}).status_code == 422


def test_operations_without_a_binding_are_refused_before_any_call(client, session, panel, fake_panel):
    second = client.post("/v1/webapp/panels", headers=as_user(ROOT_ID),
                         json={"name": "No-Binding", "base_url": "https://second.example.test", "api_key": API_KEY})
    assert second.status_code == 200, second.text
    calls = len([c for c in fake_panel if "op" in c])
    refused = client.get(f"/v1/webapp/panels/{second.json()['id']}/users", headers=as_user(ROOT_ID))
    assert refused.status_code == 409
    assert len([c for c in fake_panel if "op" in c]) == calls


def test_a_foreign_organization_cannot_drive_another_panels_server(client, session, panel, fake_panel):
    client.post("/v1/webapp/admin/resellers", headers=as_user(ROOT_ID),
                json={"name": "Staff Office", "slug": "staff", "telegram_id": STAFF_ID, "price_per_gib_irr": 2000, "credit_limit_irr": 100_000})
    calls = len(fake_panel)
    assert client.post(f"/v1/webapp/panels/{panel}/nodes/5/enable", headers=as_user(STAFF_ID)).status_code == 404
    assert client.get(f"/v1/webapp/panels/{panel}/users", headers=as_user(STAFF_ID)).status_code == 404
    assert len(fake_panel) == calls


@pytest.mark.parametrize("status_code", [401, 403])
def test_rejected_credentials_are_reported_without_the_url(client, session, panel, monkeypatch, status_code):
    recorded = []
    monkeypatch.setattr(v04_app, "Client", FakePanel(recorded, failure=status_code))
    response = client.post(f"/v1/webapp/panels/{panel}/nodes/5/reconnect", headers=as_user(ROOT_ID))
    assert response.status_code == 502
    assert response.json()["detail"] == "اعتبارنامهٔ ذخیره‌شده در PasarGuard پذیرفته نشد"
    assert URL not in response.text and "secret-token" not in response.text and API_KEY not in response.text
    assert session.scalar(text("SELECT metadata ->> 'status_code' FROM audit_logs WHERE action='node.reconnect.failed'")) == str(status_code)


def test_an_unreachable_panel_never_echoes_the_exception(client, session, panel, monkeypatch):
    recorded = []

    class Down(FakePanel):
        def reconnect_node(self, node_id):
            self.recorded.append({"op": "reconnect_node", "args": (node_id,)})
            raise httpx.ConnectError(f"failed to connect to {URL}")

    monkeypatch.setattr(v04_app, "Client", Down(recorded))
    response = client.post(f"/v1/webapp/panels/{panel}/nodes/5/reconnect", headers=as_user(ROOT_ID))
    assert response.status_code == 502 and response.json()["detail"] == "اتصال به PasarGuard برقرار نشد"
    assert URL not in response.text
    assert session.scalar(text("SELECT count(*) FROM audit_logs WHERE action='node.reconnect'")) == 0
    assert session.scalar(text("SELECT metadata FROM audit_logs WHERE action='node.reconnect.failed'"))["node_id"] == 5


def test_only_the_system_administrator_changes_the_billing_coefficient(client, session, fake_panel):
    client.post("/v1/onboarding/bootstrap", headers=as_user(ROOT_ID),
                json={"business_name": "Central Holdings", "slug": "central-holdings", "setup_token": os.environ["INITIAL_SETUP_TOKEN"]})
    client.post("/v1/webapp/admin/resellers", headers=as_user(ROOT_ID),
                json={"name": "Staff Office", "slug": "staff", "telegram_id": STAFF_ID, "price_per_gib_irr": 2000, "credit_limit_irr": 100_000})
    owned = client.post("/v1/webapp/panels", headers=as_user(STAFF_ID),
                        json={"name": "Staff Panel", "base_url": URL, "api_key": API_KEY})
    assert owned.status_code == 200, owned.text
    panel_id = owned.json()["id"]
    refused = client.post(f"/v1/webapp/panels/{panel_id}/coefficient", headers=as_user(STAFF_ID), json={"usage_coefficient": "3"})
    assert refused.status_code == 403
    assert session.scalar(select(Panel.usage_coefficient).where(Panel.id == panel_id)) == 1
    assert session.scalar(text("SELECT count(*) FROM audit_logs WHERE action='panel.coefficient'")) == 0


def test_coefficient_changes_apply_for_the_owner_and_stay_out_of_the_secret_columns(client, session, panel, fake_panel):
    body = client.get(f"/v1/webapp/panels/{panel}/nodes", headers=as_user(ROOT_ID)).text
    assert API_KEY not in body and OWNER_PASSWORD not in body
    changed = client.post(f"/v1/webapp/panels/{panel}/coefficient", headers=as_user(ROOT_ID), json={"usage_coefficient": "0.85"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["usage_coefficient"] == "0.8500"
    assert str(session.scalar(select(Panel.usage_coefficient).where(Panel.id == panel))) == "0.8500"
