"""Server registration: secrets only ever enter through the encrypted WebApp path."""
from __future__ import annotations

import json
import os

import httpx
import pytest
from sqlalchemy import select, text

from conftest import API_KEY_HEADERS, as_user
from control_plane import pasarguard, web_api
from control_plane.app import Organization, Panel
from control_plane.secrets import resolve_secret

ROOT_ID = 100001
STAFF_ID = 300007
API_KEY = "super-secret-panel-key"
OWNER_PASSWORD = "owner-password-value"


@pytest.fixture(autouse=True)
def root_workspace(client, session):
    client.post(
        "/v1/onboarding/bootstrap",
        headers=as_user(ROOT_ID),
        json={"business_name": "Central Holdings", "slug": "central-holdings", "setup_token": os.environ["INITIAL_SETUP_TOKEN"]},
    )
    return session


class FakePanel:
    """Stands in for a reachable PasarGuard panel and records how the client was built."""

    def __init__(self, recorded):
        self.recorded = recorded

    def __call__(self, base_url, api_key=None, owner_username=None, owner_password=None, verify_tls=True):
        self.recorded.append({"base_url": base_url, "api_key": api_key, "verify_tls": verify_tls})
        return self

    def nodes(self):
        return {"nodes": [{"id": 1, "name": "Germany-01"}]}

    def reconnect_node(self, node_id):
        return {"ok": True, "node": node_id}


@pytest.fixture
def fake_panel(monkeypatch):
    recorded = []
    monkeypatch.setattr(web_api, "Client", FakePanel(recorded))
    return recorded


def _register(client, **overrides):
    payload = {
        "name": "Frankfurt-01",
        "base_url": "https://panel.example.test/",
        "api_key": API_KEY,
        "owner_username": "owner",
        "owner_password": OWNER_PASSWORD,
        "pg_admin_id": 42,
        "admin_username": "billing",
        "usage_coefficient": "1.5",
    }
    payload.update(overrides)
    return client.post("/v1/webapp/panels", headers=as_user(ROOT_ID), json=payload)


def test_panel_secrets_are_encrypted_and_never_returned(client, session, fake_panel):
    response = _register(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["nodes_detected"] == 1
    assert API_KEY not in response.text and OWNER_PASSWORD not in response.text
    panel = session.scalars(select(Panel)).one()
    assert panel.base_url == "https://panel.example.test"
    assert panel.api_key_ref.startswith("enc://") and panel.owner_pass_ref.startswith("enc://")
    assert resolve_secret(panel.api_key_ref) == API_KEY
    assert resolve_secret(panel.owner_pass_ref) == OWNER_PASSWORD
    assert str(panel.usage_coefficient) == "1.5000"
    assert panel.verify_tls is True
    assert fake_panel[0]["api_key"] == API_KEY, "the connection test must use the supplied key"
    assert fake_panel[0]["verify_tls"] is True
    # The stored reference must not survive a JSON dump of the row either.
    assert API_KEY not in json.dumps({c.name: str(getattr(panel, c.name)) for c in Panel.__table__.columns})


def test_registration_refuses_secret_references_and_unknown_fields(client, session, fake_panel):
    rejected = _register(client, api_key_ref="env://PANEL_KEY")
    assert rejected.status_code == 422, rejected.text
    assert _register(client, api_key="").status_code == 422
    assert session.scalar(text("SELECT count(*) FROM panels")) == 0


def test_tls_verification_is_an_explicit_root_only_choice(client, session, fake_panel):
    # Verification is the default; opting out is a root decision, it is audited and never silent.
    assert _register(client).status_code == 200 and fake_panel[-1]["verify_tls"] is True
    plain = client.post(
        "/v1/panels",
        headers=API_KEY_HEADERS,
        json={"name": "Plain", "base_url": "http://panel.example.test", "api_key_ref": "env://PANEL_KEY"},
    )
    assert plain.status_code == 422
    client.post("/v1/admin/actor-bindings", headers=API_KEY_HEADERS,
                json={"telegram_id": STAFF_ID, "organization_id": session.scalar(select(Organization.id)),
                      "display_name": "Staff", "role": "reseller_admin"})
    refused = client.post("/v1/webapp/panels", headers=as_user(STAFF_ID),
                          json={"name": "Staff panel", "base_url": "https://staff.example.test",
                                "api_key": "staff-panel-key", "verify_tls": False})
    assert refused.status_code == 403, refused.text
    assert session.scalar(text("SELECT count(*) FROM panels WHERE base_url='https://staff.example.test'")) == 0
    allowed = client.post("/v1/webapp/panels", headers=as_user(ROOT_ID),
                          json={"name": "Lab panel", "base_url": "https://lab.example.test",
                                "api_key": "root-panel-key", "verify_tls": False})
    assert allowed.status_code == 200, allowed.text
    assert fake_panel[-1]["verify_tls"] is False
    assert session.scalar(
        text("SELECT count(*) FROM audit_logs WHERE action='panel.create' AND metadata->>'verify_tls'='false'")) == 1


def test_control_api_cannot_write_without_the_key(client, session):
    response = client.post("/v1/panels", headers={"X-Control-Key": ""}, json={"name": "No Key", "base_url": "https://panel.example.test"})
    assert response.status_code == 401
    assert session.scalar(text("SELECT count(*) FROM panels")) == 0


def test_credentials_are_never_accepted_inside_the_url(client, fake_panel):
    for base_url in ("https://user:pass@panel.example.test", "https:///panel.example.test", "ftp://panel.example.test"):
        response = _register(client, base_url=base_url)
        assert response.status_code == 422, base_url
    assert fake_panel == []


def test_panel_client_does_not_follow_redirects(monkeypatch):
    calls = []
    real_client = httpx.Client

    def recording_client(*args, **kwargs):
        calls.append(kwargs)
        return real_client(transport=httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "http://elsewhere.test"})), **kwargs)

    monkeypatch.setattr(pasarguard.httpx, "Client", recording_client)
    client = pasarguard.Client("https://panel.example.test", API_KEY)
    with pytest.raises(httpx.HTTPStatusError):
        client.nodes()
    assert calls and not calls[0].get("follow_redirects"), "a redirect could move the API key to another host"


def test_panel_listing_and_nodes_never_expose_secret_columns(client, session, fake_panel):
    _register(client)
    listed = client.get("/v1/webapp/panels", headers=as_user(ROOT_ID)).json()
    assert listed[0]["base_url"] == "https://panel.example.test"
    assert set(listed[0]) == {"id", "name", "base_url", "status"}
    nodes = client.get(f"/v1/webapp/panels/{listed[0]['id']}/nodes", headers=as_user(ROOT_ID)).json()
    assert nodes["nodes"] == [{"id": 1, "name": "Germany-01"}]
    assert API_KEY not in json.dumps(listed) + json.dumps(nodes)
