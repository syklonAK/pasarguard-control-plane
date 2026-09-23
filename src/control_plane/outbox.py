"""Reliable delivery of domain events: claim, act, retry with backoff, never duplicate.

Every side effect that must survive a crash (suspending a reseller on the PasarGuard
panel, notifying a Telegram actor) is queued as an outbox row inside the same
transaction that changed the ledger, then drained by the worker.
"""
from __future__ import annotations

import json
import os
import time
from datetime import timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select

from .app import Actor, Binding, Membership, Organization, Outbox, Panel, SessionLocal, now, uid

MAX_ATTEMPTS = int(os.getenv("OUTBOX_MAX_ATTEMPTS", "8"))
BASE_BACKOFF_SECONDS = int(os.getenv("OUTBOX_BACKOFF_SECONDS", "15"))
MAX_BACKOFF_SECONDS = int(os.getenv("OUTBOX_MAX_BACKOFF_SECONDS", "900"))
# PasarGuard refuses further traffic once the admin exceeds this byte ceiling.
HOLD_LIMIT_BYTES = int(os.getenv("HOLD_LIMIT_BYTES", "1"))
CLAIM_TIMEOUT_SECONDS = int(os.getenv("OUTBOX_CLAIM_TIMEOUT_SECONDS", "300"))


def _json(value) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value))


def enqueue(s, topic: str, aggregate_id: str, payload: dict):
    """Queue an event, skipping a duplicate that is still waiting for the same aggregate."""
    if s.scalar(select(Outbox.id).where(Outbox.topic == topic, Outbox.aggregate_id == aggregate_id, Outbox.status == "pending")):
        return None
    event = Outbox(id=uid(), topic=topic, aggregate_id=aggregate_id, payload=json.dumps(payload, default=_json))
    s.add(event)
    return event


def claim(s, worker_id: str, limit: int = 10):
    rows = s.scalars(
        select(Outbox)
        .where(Outbox.status == "pending", Outbox.next_attempt_at <= now())
        .order_by(Outbox.next_attempt_at, Outbox.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    for event in rows:
        event.status = "claimed"
        event.claimed_by = worker_id
        event.claimed_at = now()
    if rows:
        s.commit()
    return rows


def complete(s, event) -> None:
    event.status = "done"
    event.completed_at = now()
    event.last_error = None
    s.commit()


def retry(s, event, error: str) -> str:
    """Return the state the event fell back to: 'pending' or 'dead'."""
    event.attempts += 1
    event.last_error = str(error)[:2000]
    event.claimed_by = None
    event.claimed_at = None
    if event.attempts >= MAX_ATTEMPTS:
        event.status = "dead"
        event.completed_at = now()
    else:
        event.status = "pending"
        event.next_attempt_at = now() + timedelta(seconds=min(BASE_BACKOFF_SECONDS * 2 ** (event.attempts - 1), MAX_BACKOFF_SECONDS))
    s.commit()
    return event.status


def notify(text_message: str, chat_ids) -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    sent = 0
    with httpx.Client(timeout=15) as client:
        for chat_id in chat_ids:
            response = client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                   json={"chat_id": chat_id, "text": text_message, "parse_mode": "HTML"})
            response.raise_for_status()
            sent += 1
    return sent


def audience(s, organization_id: str):
    return list(s.execute(
        select(Actor.telegram_id)
        .join(Membership, Membership.actor_id == Actor.id)
        .where(Membership.organization_id == organization_id, Membership.status == "active", Actor.status == "active")
    ).scalars().all())


def panel_client(s, panel: Panel):
    from .pasarguard import Client
    from .secrets import resolve_secret

    return Client(panel.base_url, resolve_secret(panel.api_key_ref), resolve_secret(panel.owner_user_ref),
                  resolve_secret(panel.owner_pass_ref), panel.verify_tls)


def money(amount_irr) -> str:
    return f"{int(amount_irr or 0):,}"


def handle_billing_hold(s, event, notifier=notify, client_for=panel_client):
    binding = s.get(Binding, event.aggregate_id)
    if not binding:
        raise RuntimeError("binding no longer exists")
    payload = json.loads(event.payload)
    panel = s.get(Panel, binding.panel_id)
    if panel and panel.status == "active" and binding.status == "active":
        client_for(s, panel).modify_admin(binding.pg_admin_id, {"limit_use_in_bytes": HOLD_LIMIT_BYTES})
        binding.status = "suspended"
    notifier(
        "⛔ <b>مصرف شما از سقف اعتبار عبور کرد</b>\n"
        f"تسویه معوق: {money(payload.get('amount_irr'))} ریال\n"
        "برای ادامه اتصال، از منوی مالی درخواست اعتبار ثبت کنید.",
        audience(s, binding.organization_id),
    )
    s.commit()


def handle_usage_reset(s, event, notifier=notify):
    payload = json.loads(event.payload)
    organization = s.get(Organization, payload["organization_id"])
    if not organization:
        raise RuntimeError("organization no longer exists")
    notifier(
        "🔄 <b>شمارنده مصرف سرور پاسارگارد ریست شد</b>\n"
        f"سازمان: {organization.name}\n"
        "مبنا از نو ثبت شد؛ مصرف پیش از ریست دوباره صورتحساب نمی‌شود.",
        audience(s, organization.id),
    )
    s.commit()


def handle_approval_granted(s, event, notifier=notify):
    payload = json.loads(event.payload)
    chat_ids = [payload["chat_id"]] if payload.get("chat_id") else []
    action = payload.get("action", "")
    note = payload.get("note", "")
    status = payload.get("status", "approved")
    icon = "✅" if status == "approved" else "⛔"
    notifier(f"{icon} پاسخ درخواست <b>{action}</b>: {status}" + (f"\n{note}" if note else ""), chat_ids)
    s.commit()


def handle_support_ticket(s, event, notifier=notify):
    payload = json.loads(event.payload)
    roots = [int(os.environ["ROOT_TELEGRAM_ID"])] if os.getenv("ROOT_TELEGRAM_ID", "").isdigit() else []
    if not roots:
        raise RuntimeError("ROOT_TELEGRAM_ID is not configured")
    notifier(f"🎟 تیکت پشتیبانی از {payload.get('organization', '')}: {payload.get('message', '')}", roots)
    s.commit()


HANDLERS = {
    "billing.hold_required": handle_billing_hold,
    "usage.reset_detected": handle_usage_reset,
    "approval.granted": handle_approval_granted,
    "support.ticket": handle_support_ticket,
}


def reclaim_stale(s, older_than_seconds: int = CLAIM_TIMEOUT_SECONDS) -> int:
    """A worker that died mid-flight must not strand the events it claimed."""
    cutoff = now() - timedelta(seconds=older_than_seconds)
    stale = s.scalars(select(Outbox).where(Outbox.status == "claimed", Outbox.claimed_at <= cutoff)).all()
    for event in stale:
        event.status = "pending"
        event.claimed_by = None
        event.claimed_at = None
    if stale:
        s.commit()
    return len(stale)


def run_once(worker_id: str | None = None, limit: int = 10, handlers=HANDLERS) -> dict:
    worker_id = worker_id or f"{os.getenv('HOSTNAME', 'worker')}-{int(time.time())}"
    stats = {"claimed": 0, "done": 0, "retry": 0, "dead": 0}
    with SessionLocal() as s:
        stats["stale"] = reclaim_stale(s)
        for event in claim(s, worker_id, limit):
            stats["claimed"] += 1
            handler = handlers.get(event.topic)
            if not handler:
                stats["retry" if retry(s, event, f"no handler for topic {event.topic}") == "pending" else "dead"] += 1
                continue
            try:
                handler(s, event)
                complete(s, event)
                stats["done"] += 1
            except Exception as exc:
                s.rollback()
                stats["retry" if retry(s, event, f"{type(exc).__name__}: {exc}") == "pending" else "dead"] += 1
    return stats


def main():
    interval = int(os.getenv("OUTBOX_POLL_SECONDS", "10"))
    while True:
        stats = run_once()
        if stats["claimed"]:
            print(f"outbox: {stats}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
