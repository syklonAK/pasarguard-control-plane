"""The webhook gateway is the only door Telegram knocks on: it must filter replays."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import control_plane.telegram_gateway as gateway

SECRET = gateway.os.environ["TELEGRAM_WEBHOOK_SECRET"]


class FakeRedis:
    def __init__(self):
        self.keys = set()
        self.stream = []
        self.fails = 0

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True

    async def delete(self, key):
        self.keys.discard(key)

    async def xadd(self, name, fields, maxlen=None, approximate=False):
        if self.fails:
            self.fails -= 1
            raise RuntimeError("redis is down")
        self.stream.append((name, fields))
        return f"{len(self.stream)}-0"


@pytest.fixture
def redis_client():
    return FakeRedis()


@pytest.fixture
def hook(monkeypatch, redis_client):
    monkeypatch.setattr(gateway, "redis", redis_client)
    with TestClient(gateway.app) as client:
        client.headers["X-Telegram-Bot-Api-Secret-Token"] = SECRET
        yield client, redis_client


def post(client, payload):
    return client.post("/telegram/webhook", json=payload)


def test_valid_update_reaches_the_stream(hook):
    client, redis_client = hook
    response = post(client, {"update_id": 5001, "message": {"text": "/start"}})
    assert response.status_code == 200 and response.json() == {"ok": True}
    name, fields = redis_client.stream[0]
    assert name == "telegram_updates" and json.loads(fields["update"])["update_id"] == 5001


def test_replayed_update_id_is_accepted_but_not_requeued(hook):
    client, redis_client = hook
    update = {"update_id": 5002, "message": {"text": "/id"}}
    assert post(client, update).json() == {"ok": True}
    assert post(client, update).json() == {"ok": True, "duplicate": True}
    assert len(redis_client.stream) == 1


@pytest.mark.parametrize("sent_secret", ["", "wrong"], ids=["missing", "wrong"])
def test_wrong_secret_never_queues(hook, sent_secret):
    client, redis_client = hook
    if sent_secret:
        client.headers["X-Telegram-Bot-Api-Secret-Token"] = sent_secret
    else:
        client.headers.pop("X-Telegram-Bot-Api-Secret-Token")
    assert post(client, {"update_id": 5003}).status_code == 401
    assert redis_client.stream == []


def test_update_without_id_is_rejected(hook):
    client, redis_client = hook
    assert post(client, {"message": {"text": "hi"}}).status_code == 422
    assert redis_client.stream == []


def test_failed_queue_write_frees_the_update_id(hook):
    client, redis_client = hook
    redis_client.fails = 1
    update = {"update_id": 5004, "message": {"text": "/menu"}}
    with pytest.raises(RuntimeError):
        post(client, update)
    assert "tg:update:5004" not in redis_client.keys
    # Telegram retries the delivery, and that retry has to be queued rather than
    # swallowed by the dedupe key the failed attempt had already written.
    assert post(client, update).json() == {"ok": True}
    assert len(redis_client.stream) == 1
