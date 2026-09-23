"""Outbox delivery: hold policy, retry/backoff, dedupe and stale claim recovery."""
from __future__ import annotations


import pytest
from sqlalchemy import func, select, text

from control_plane import outbox
from control_plane.app import Binding, Outbox, Usage, UsageIn, observe
from control_plane.domain import GIB


class Recorder:
    def __init__(self, fail=False):
        self.messages = []
        self.panel_calls = []
        self.fail = fail

    def notify(self, message, chat_ids):
        if self.fail:
            raise RuntimeError("telegram unreachable")
        self.messages.append((message, list(chat_ids)))
        return len(list(chat_ids))

    def client_for(self, session, panel):
        recorder = self

        class FakeClient:
            def modify_admin(self, admin_id, payload):
                recorder.panel_calls.append((admin_id, payload))

        return FakeClient()

    def handlers(self):
        return {
            "billing.hold_required": lambda s, e: outbox.handle_billing_hold(s, e, notifier=self.notify, client_for=self.client_for),
            "usage.reset_detected": lambda s, e: outbox.handle_usage_reset(s, e, notifier=self.notify),
            "approval.granted": lambda s, e: outbox.handle_approval_granted(s, e, notifier=self.notify),
        }


def _events(session, topic=None):
    query = select(Outbox)
    if topic:
        query = query.where(Outbox.topic == topic)
    return session.scalars(query.order_by(Outbox.created_at)).all()


@pytest.fixture
def reseller(session, make_org, make_actor, make_binding):
    root = make_org(credit_limit_irr=0)
    leaf = make_org(root, price_per_gib_irr=1000, credit_limit_irr=10_000)
    make_actor(200002, leaf)
    binding = make_binding(leaf)
    return {"root": root, "leaf": leaf, "binding": binding, "panel_id": binding.panel_id}


def _trigger_hold(session, reseller, amount=25_000):
    from control_plane.app import record_hold

    record_hold(session, reseller["binding"].id, reseller["leaf"].id, amount)
    session.commit()


def test_hold_event_is_queued_once_per_binding(session, reseller):
    _trigger_hold(session, reseller)
    _trigger_hold(session, reseller)
    _trigger_hold(session, reseller)
    assert len(_events(session, "billing.hold_required")) == 1


def test_dispatcher_suspends_binding_and_notifies_the_reseller(session, reseller):
    _trigger_hold(session, reseller)
    recorder = Recorder()
    stats = outbox.run_once(handlers=recorder.handlers())
    session.expire_all()
    assert stats["done"] == 1
    assert session.get(Binding, reseller["binding"].id).status == "suspended"
    assert recorder.panel_calls == [(7, {"limit_use_in_bytes": outbox.HOLD_LIMIT_BYTES})]
    assert recorder.messages and recorder.messages[0][1] == [200002]
    assert "سقف اعتبار" in recorder.messages[0][0]
    event = _events(session, "billing.hold_required")[0]
    assert event.status == "done"
    assert event.completed_at is not None
    # The worker only polls active bindings, so a suspended one stops accumulating charges.
    assert session.get(Binding, reseller["binding"].id).id not in [
        b.id for b in session.scalars(select(Binding).where(Binding.status == "active")).all()
    ]


def test_delivery_failure_retries_with_backoff_and_becomes_dead(session, reseller, monkeypatch):
    _trigger_hold(session, reseller)
    recorder = Recorder(fail=True)
    monkeypatch.setattr(outbox, "MAX_ATTEMPTS", 2)
    outbox.run_once(handlers=recorder.handlers())
    session.expire_all()
    event = _events(session, "billing.hold_required")[0]
    assert event.status == "pending" and event.attempts == 1
    assert "telegram unreachable" in event.last_error
    assert event.next_attempt_at > outbox.now()
    assert outbox.run_once(handlers=recorder.handlers())["claimed"] == 0, "backoff window must be respected"
    session.execute(text("UPDATE outbox SET next_attempt_at=now() WHERE id=:id"), {"id": event.id})
    session.commit()
    outbox.run_once(handlers=recorder.handlers())
    session.expire_all()
    event = _events(session, "billing.hold_required")[0]
    assert event.status == "dead" and event.attempts == 2
    assert session.get(Binding, reseller["binding"].id).status == "active"


def test_usage_reset_observation_emits_one_event(session, reseller):
    from control_plane.app import RESET_CONFIRM_READINGS

    binding = reseller["binding"]
    observe(session, UsageIn(binding_id=binding.id, lifetime_bytes=5 * GIB, observed_at=outbox.now()))
    session.commit()
    for _ in range(RESET_CONFIRM_READINGS):
        observe(session, UsageIn(binding_id=binding.id, lifetime_bytes=1 * GIB, observed_at=outbox.now()))
    session.commit()
    events = _events(session, "usage.reset_detected")
    assert len(events) == 1
    assert session.scalar(select(func.count()).select_from(Usage).where(Usage.status == "reset_baseline")) == 1
    recorder = Recorder()
    outbox.run_once(handlers=recorder.handlers())
    assert recorder.messages and "ریست" in recorder.messages[0][0]


def test_stale_claims_return_to_the_queue(session, reseller):
    _trigger_hold(session, reseller)
    event_id = _events(session, "billing.hold_required")[0].id
    session.execute(
        text("UPDATE outbox SET status='claimed', claimed_by='dead-worker', claimed_at=now()-interval '1 hour' WHERE id=:id"),
        {"id": event_id},
    )
    session.commit()
    assert outbox.run_once(handlers=Recorder(fail=True).handlers())["stale"] == 1
    session.expire_all()
    assert _events(session, "billing.hold_required")[0].status == "pending"


def test_unknown_topic_is_retried_not_dropped(session, reseller):
    session.add(Outbox(id="0" * 32, topic="not.implemented", aggregate_id=reseller["binding"].id, payload="{}"))
    session.commit()
    assert outbox.run_once(handlers={})["retry"] == 1
    session.expire_all()
    event = session.get(Outbox, "0" * 32)
    assert event.status == "pending" and "no handler" in event.last_error
