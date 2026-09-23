"""The bot must drive the same audited panel operations as the WebApp, with Persian replies."""
from __future__ import annotations

import os

import httpx
import pytest
from sqlalchemy import text

from conftest import as_user
from control_plane import telegram_consumer, v04_app

ROOT_ID = 100001
API_KEY = "bot-panel-key"
URL = "https://panel.example.test"


class FakePanel:
    def __init__(self, recorded, failure=None):
        self.recorded = recorded
        self.failure = failure

    def __call__(self, *args, **kwargs):
        self.recorded.append({"init": args})
        return self

    def _op(self, name, *args):
        self.recorded.append({"op": name, "args": args})
        if self.failure is not None:
            request = httpx.Request("GET", f"{URL}/api/x")
            raise httpx.HTTPStatusError("boom", request=request, response=httpx.Response(self.failure, request=request))
        return {"nodes": [{"id": 1, "name": "Frankfurt-A", "enable": True, "message": "connected"}]}

    def nodes(self):
        return self._op("nodes")

    def node_status(self, node_id):
        return self._op("node_status", node_id)

    def set_node_enabled(self, node_id, enabled):
        return self._op("set_node_enabled", node_id, enabled)

    def reconnect_node(self, node_id):
        return self._op("reconnect_node", node_id)

    def reset_node(self, node_id):
        return self._op("reset_node", node_id)


@pytest.fixture
def fake_panel(monkeypatch):
    recorded = []
    monkeypatch.setattr(v04_app, "Client", FakePanel(recorded))
    return recorded


@pytest.fixture
def bot_panel(client, session, fake_panel):
    client.post(
        "/v1/onboarding/bootstrap",
        headers=as_user(ROOT_ID),
        json={"business_name": "Central Holdings", "slug": "central-holdings", "setup_token": os.environ["INITIAL_SETUP_TOKEN"]},
    )
    created = client.post("/v1/webapp/panels", headers=as_user(ROOT_ID),
                          json={"name": "Frankfurt-01", "base_url": URL, "api_key": API_KEY})
    assert created.status_code == 200, created.text
    fake_panel.clear()
    return created.json()["id"]


def ops(recorded):
    return [entry["op"] for entry in recorded if "op" in entry]


def args_of(recorded, name):
    return next(entry["args"] for entry in reversed(recorded) if entry.get("op") == name)


def test_node_listing_shows_persian_controls_and_audits_the_read(client, session, bot_panel, fake_panel):
    message, markup = telegram_consumer.node_list(ROOT_ID, bot_panel)
    assert "Frankfurt-A" in message and "نودهای Frankfurt-01" in message and "🟢" in message
    assert [button["callback_data"] for row in markup["inline_keyboard"] for button in row] == [
        f"node|{bot_panel}|1|reconnect", f"node|{bot_panel}|1|disable",
        f"node|{bot_panel}|1|reset", f"node|{bot_panel}|1|status"]
    assert session.scalar(text("SELECT count(*) FROM audit_logs WHERE action='panel.nodes.read'")) == 1


@pytest.mark.parametrize("action,client_call,arg", [
    ("enable", "set_node_enabled", True),
    ("disable", "set_node_enabled", False),
    ("reconnect", "reconnect_node", None),
    ("reset", "reset_node", None),
])
def test_every_node_action_is_audited_once(client, session, bot_panel, fake_panel, action, client_call, arg):
    before = ops(fake_panel)
    reply = telegram_consumer.node_action(ROOT_ID, bot_panel, "3", action)
    assert "نود" in reply or "اتصال" in reply
    assert args_of(fake_panel, client_call) == ((3, arg) if arg is not None else (3,))
    assert session.scalar(text(f"SELECT count(*) FROM audit_logs WHERE action='node.{action}'")) == 1
    assert ops(fake_panel)[len(before):] == [client_call]


def test_node_status_replies_with_the_panel_payload(client, session, bot_panel, fake_panel):
    body = telegram_consumer.node_action(ROOT_ID, bot_panel, "3", "status")
    assert "Frankfurt-A" in body and args_of(fake_panel, "node_status") == (3,)
    assert session.scalar(text("SELECT count(*) FROM audit_logs WHERE action='node.status.read'")) == 1


def test_an_unlinked_telegram_account_cannot_reach_a_server(client, session, bot_panel, fake_panel):
    with pytest.raises(PermissionError):
        telegram_consumer.node_action(999999, bot_panel, "3", "reconnect")
    assert ops(fake_panel) == []


def test_a_panel_owned_by_someone_else_is_invisible(client, session, bot_panel, fake_panel):
    with pytest.raises(PermissionError) as caught:
        telegram_consumer.node_action(ROOT_ID, "11111111-1111-1111-1111-111111111111", "3", "reconnect")
    assert "سرور پیدا نشد" in str(caught.value)
    assert ops(fake_panel) == []


def test_upstream_credentials_failure_becomes_a_persian_permission_error(client, session, bot_panel, monkeypatch):
    recorded = []
    monkeypatch.setattr(v04_app, "Client", FakePanel(recorded, failure=401))
    with pytest.raises(PermissionError) as caught:
        telegram_consumer.node_action(ROOT_ID, bot_panel, "3", "reconnect")
    assert caught.value.args[0] == "اعتبارنامهٔ ذخیره‌شده در PasarGuard پذیرفته نشد"
    assert URL not in caught.value.args[0]
    assert session.scalar(text("SELECT count(*) FROM audit_logs WHERE action='node.reconnect.failed'")) == 1


def test_an_unknown_action_never_opens_a_database_session(client, bot_panel, fake_panel):
    with pytest.raises(PermissionError):
        telegram_consumer.node_action(ROOT_ID, bot_panel, "3", "drop-table")
    assert ops(fake_panel) == []
