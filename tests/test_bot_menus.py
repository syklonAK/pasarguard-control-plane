"""Both Telegram keyboards are generated from the same permission matrix the API enforces."""
from __future__ import annotations

import pytest

from conftest import as_user
from control_plane import rbac, telegram_consumer
from control_plane.app import Actor, Membership

ROOT_ID = 100001


@pytest.fixture
def workspace(client, session, make_org, make_actor):
    client.post(
        "/v1/onboarding/bootstrap",
        headers=as_user(ROOT_ID),
        json={"business_name": "Central Holdings", "slug": "central-holdings", "setup_token": "s" * 40},
    )
    root = session.query(Actor).join(Membership, Membership.actor_id == Actor.id).first()
    return root


def staff(session, make_org, make_actor, role, telegram_id):
    org = make_org()
    make_actor(telegram_id, org, role)
    return org


def test_menu_only_offers_sections_the_role_may_open():
    for role in rbac.ROLES:
        for name in telegram_consumer.sections_for(role):
            assert rbac.can(role, telegram_consumer.SECTION_PERMISSION[name]), (role, name)
    offered = set()
    for role in rbac.ROLES:
        offered |= set(telegram_consumer.sections_for(role))
    assert offered == set(telegram_consumer.SECTION_PERMISSION), "every section must be reachable by someone"


def test_keyboard_buttons_are_valid_and_unique():
    labels = [item[1] for item in telegram_consumer.SECTIONS]
    assert len(set(labels)) == len(labels), "one label must never resolve to two sections"
    for role in rbac.ROLES:
        rendered = []
        for row in telegram_consumer.menu_for(role)["keyboard"]:
            for button in row:
                label = button["text"]
                rendered.append(label)
                assert label in telegram_consumer.LABELS.values()
                assert label_to_permission(label) in rbac.permissions_for(role)
                assert len(label.encode()) <= 64
        assert len(set(rendered)) == len(rendered)
        for row in telegram_consumer.section_menu(role)["inline_keyboard"]:
            for button in row:
                assert button["callback_data"].startswith("section|")
                assert len(button["callback_data"].encode()) <= 64


def label_to_permission(label):
    return telegram_consumer.SECTION_PERMISSION[telegram_consumer.label_to_section(label)]


def test_every_section_permission_is_a_real_permission():
    assert set(telegram_consumer.SECTION_PERMISSION.values()) <= set(rbac.ALL_PERMISSIONS)
    assert len(telegram_consumer.SECTION_PERMISSION) == len(telegram_consumer.SECTIONS)


def test_support_and_viewer_roles_get_a_smaller_menu_than_admin():
    assert "💰 درخواست‌های مالی" not in str(telegram_consumer.menu_for("viewer"))
    assert "🖥 سرورها" not in str(telegram_consumer.menu_for("support"))
    assert "⚙️ مدیریت سیستم" in str(telegram_consumer.menu_for("system_admin"))
    admin = set(telegram_consumer.sections_for("system_admin"))
    assert set(telegram_consumer.sections_for("operator")) < admin


def test_root_account_is_promoted_even_with_a_viewer_membership(session, workspace, make_org, make_actor):
    # The bootstrap account keeps reseller_admin; the configured root still resolves to system_admin.
    org = make_org()
    make_actor(ROOT_ID + 77, org, "viewer")
    assert telegram_consumer.context(ROOT_ID + 77)["role"] == "viewer"
    assert telegram_consumer.context(ROOT_ID)["role"] == "system_admin"


@pytest.mark.parametrize(
    "role,func,telegram_id",
    [("viewer", "funding_requests", 500101), ("support", "funding_requests", 500102),
     ("reseller_admin", "approval_requests", 500103), ("operator", "approval_requests", 500104)],
)
def test_unprivileged_roles_are_refused_before_any_query(
    client, session, workspace, make_org, make_actor, role, func, telegram_id
):
    staff(session, make_org, make_actor, role, telegram_id)
    with pytest.raises(PermissionError) as caught:
        getattr(telegram_consumer, func)(telegram_id)
    assert "اجازه" in str(caught.value) or "دسترسی" in str(caught.value)


def test_a_pending_approval_only_opens_wallet_adjustment_for_system_admin(
    client, session, workspace, make_org, make_actor
):
    org = make_org()
    make_actor(700001, org, "finance")
    with pytest.raises(PermissionError):
        telegram_consumer.approval_requests(700001)
    text, markup = telegram_consumer.approval_requests(ROOT_ID)
    assert "درخواست تأیید بازی وجود ندارد" in text and markup is None


class FakeRedis:
    def __init__(self):
        self.state = {}

    async def setex(self, key, ttl, value):
        self.state[key] = value

    async def get(self, key):
        return self.state.get(key)

    async def delete(self, key):
        self.state.pop(key, None)


def test_every_offered_section_renders_instead_of_falling_through(
    client, session, workspace, monkeypatch
):
    import asyncio

    calls = []

    async def record(chat_id, message, markup=None):
        calls.append((message, markup))

    async def fake_role(chat_id):
        return "system_admin"

    monkeypatch.setattr(telegram_consumer, "send", record)
    monkeypatch.setattr(telegram_consumer, "current_role", fake_role)
    for name in telegram_consumer.sections_for("system_admin"):
        calls.clear()
        asyncio.run(telegram_consumer.open_section(ROOT_ID, name, FakeRedis()))
        assert calls, name
        assert "گزینه معتبر" not in calls[0][0], name
