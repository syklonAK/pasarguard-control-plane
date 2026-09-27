"""The customer funnel: a Telegram chat orders a panel, an admin quotes it, payment creates an account.

The WebApp API and the bot drive the same core, so these tests follow one order from first touch to
a funded organization and assert that nothing exists before an admin has priced it.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import select, text

from conftest import as_user
from control_plane import telegram_consumer, web_api
from control_plane.app import Actor, Membership

ROOT_ID = 100001
CUSTOMER_ID = 900001
STRANGER_ID = 900002
OTHER_MANAGER_ID = 300002
API_KEY = "order-panel-key"
URL = "https://panel.example.test"
QUOTE = {"amount_irr": 50_000_000, "credit_limit_irr": 10_000_000, "price_per_gib_irr": 12_000,
         "payment_instructions": "کارت 6037-9988-1122-3344 به نام مجموعه مرکزی", "pg_admin_id": 77}


class FakePanel:
    """The registration handshake only; the funnel itself never calls the panel."""

    def __call__(self, *args, **kwargs):
        return self

    def nodes(self):
        return {"nodes": []}


@pytest.fixture
def seller(client, session, monkeypatch):
    """A bootstrapped workspace whose only server is on the customer order list."""
    monkeypatch.setattr(web_api, "Client", FakePanel())
    client.post("/v1/onboarding/bootstrap", headers=as_user(ROOT_ID),
                json={"business_name": "Central Holdings", "slug": "central-holdings",
                      "setup_token": os.environ["INITIAL_SETUP_TOKEN"]})
    panel = client.post("/v1/webapp/panels", headers=as_user(ROOT_ID),
                        json={"name": "Frankfurt-01", "base_url": f"{URL}/", "api_key": API_KEY,
                              "owner_username": "owner", "owner_password": "owner-secret", "pg_admin_id": 42}
                        ).json()["id"]
    assert client.post(f"/v1/webapp/panels/{panel}/saleable", params={"saleable": "true"},
                       headers=as_user(ROOT_ID)).status_code == 200
    return panel


def order(client, panel, user_id=CUSTOMER_ID, **overrides):
    payload = {"panel_id": panel, "business_name": "North Sales", "user_count": 50, "daily_gib": 3,
               "note": "فروش شمال"}
    payload.update(overrides)
    return client.post("/v1/webapp/orders", headers=as_user(user_id), json=payload)


def test_an_unlinked_chat_can_see_the_servers_on_sale(client, seller):
    options = client.get("/v1/webapp/order-options", headers=as_user(STRANGER_ID))
    assert options.status_code == 200, options.text
    assert [panel["name"] for panel in options.json()["panels"]] == ["Frankfurt-01"]


def test_a_pending_order_creates_no_organization_and_no_money(client, session, seller):
    created = order(client, seller)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "pending" and body["organization_id"] is None
    assert session.scalar(text("SELECT count(*) FROM organizations")) == 1
    assert session.scalar(text("SELECT count(*) FROM transactions")) == 0
    assert session.scalar(text("SELECT count(*) FROM panel_orders WHERE id=:id"), {"id": body["id"]}) == 1


def test_a_server_the_owner_did_not_put_on_sale_cannot_be_ordered(client, session, seller):
    assert client.post(f"/v1/webapp/panels/{seller}/saleable", params={"saleable": "false"},
                       headers=as_user(ROOT_ID)).status_code == 200
    refused = order(client, seller)
    assert refused.status_code == 422
    assert "برای سفارش باز نیست" in refused.json()["detail"]
    assert session.scalar(text("SELECT count(*) FROM panel_orders")) == 0


def test_the_funnel_refuses_to_skip_a_state(client, seller):
    body = order(client, seller).json()
    early = client.post(f"/v1/webapp/orders/{body['id']}/payment", headers=as_user(CUSTOMER_ID))
    assert early.status_code == 409
    quote = client.post(f"/v1/webapp/orders/{body['id']}/quote", headers=as_user(ROOT_ID), json=QUOTE)
    assert quote.status_code == 200 and quote.json()["status"] == "quoted"
    assert client.post(f"/v1/webapp/orders/{body['id']}/approve", headers=as_user(ROOT_ID)).status_code == 409


def test_only_the_owner_of_the_ordered_server_can_price_it(client, session, seller, make_org, make_actor):
    other = make_org()
    make_actor(OTHER_MANAGER_ID, other, "reseller_admin")
    body = order(client, seller).json()
    refused = client.post(f"/v1/webapp/orders/{body['id']}/quote", headers=as_user(OTHER_MANAGER_ID), json=QUOTE)
    assert refused.status_code == 403
    assert client.get("/v1/webapp/orders/queue", headers=as_user(OTHER_MANAGER_ID)).json() == []
    assert len(client.get("/v1/webapp/orders/queue", headers=as_user(ROOT_ID)).json()) == 1


def test_a_full_order_becomes_a_funded_customer_organization(client, session, seller):
    order_id = order(client, seller).json()["id"]
    client.post(f"/v1/webapp/orders/{order_id}/quote", headers=as_user(ROOT_ID), json=QUOTE)
    client.post(f"/v1/webapp/orders/{order_id}/confirm", headers=as_user(CUSTOMER_ID))
    client.post(f"/v1/webapp/orders/{order_id}/payment", headers=as_user(CUSTOMER_ID))
    approved = client.post(f"/v1/webapp/orders/{order_id}/approve", headers=as_user(ROOT_ID))
    assert approved.status_code == 200, approved.text
    result = approved.json()
    assert result["balance_irr"] == QUOTE["amount_irr"] and result["bound"] is True
    assert session.scalar(text("SELECT count(*) FROM ledger_imbalance")) == 0
    membership = session.scalar(text("SELECT role FROM memberships WHERE organization_id=:org"), {"org": result["organization_id"]})
    assert membership == "customer"
    capabilities = client.get("/v1/webapp/capabilities", headers=as_user(CUSTOMER_ID)).json()
    assert capabilities["role"] == "customer"
    assert capabilities["permissions"]["manage_servers"] is False
    assert capabilities["permissions"]["order_panel"] is True
    assert client.post("/v1/webapp/panels", headers=as_user(CUSTOMER_ID),
                       json={"name": "rogue", "base_url": "https://elsewhere.test", "api_key": "x" * 12}
                       ).status_code == 403


def test_a_rejected_order_never_reaches_the_ledger(client, session, seller):
    order_id = order(client, seller).json()["id"]
    rejected = client.post(f"/v1/webapp/orders/{order_id}/reject", headers=as_user(ROOT_ID),
                           json={"reason": "مصرف روزانه با ظرفیت سرور نمی‌خواند"})
    assert rejected.json()["status"] == "rejected"
    assert session.scalar(text("SELECT count(*) FROM transactions")) == 0
    assert session.scalar(text("SELECT count(*) FROM organizations")) == 1


# --------------------------------------------------------------------------------------
# The same funnel through the Telegram bot
# --------------------------------------------------------------------------------------

def bot_quote_data(order_id):
    return {"order_id": order_id, "amount_irr": QUOTE["amount_irr"], "credit_limit_irr": QUOTE["credit_limit_irr"],
            "price_per_gib_irr": QUOTE["price_per_gib_irr"], "instructions": QUOTE["payment_instructions"],
            "pg_admin_id": QUOTE["pg_admin_id"]}


def menu_labels(role):
    return [button["text"] for row in telegram_consumer.menu_for(role)["keyboard"] for button in row]


def markup_labels(markup):
    return [button["text"] for row in (markup or {}).get("inline_keyboard", []) for button in row]


def test_the_customer_menu_orders_and_never_operates_a_server():
    labels = menu_labels("customer")
    assert "➕ سفارش پنل" in labels and "🧾 سفارش‌های من" in labels
    for forbidden in ("🖥 سرورها", "👥 نمایندگان", "➕ نماینده جدید", "⚙️ مدیریت سیستم", "🌐 وب‌اپ"):
        assert forbidden not in labels
    offered = {telegram_consumer.SECTION_PERMISSION[name] for name in telegram_consumer.sections_for("customer")}
    assert offered.isdisjoint({"manage_servers", "decide_orders", "decide_funding", "manage_resellers", "view_audit"})


def test_a_chat_with_no_membership_at_all_resolves_to_the_customer_menu():
    assert asyncio.run(telegram_consumer.current_role(STRANGER_ID)) == "customer"
    assert "🧾 سفارش‌های پنل" not in menu_labels("customer")


def test_the_staff_menu_offers_the_order_queue_to_whoever_may_decide_it():
    assert "🧾 سفارش‌های پنل" in menu_labels("system_admin")
    assert "🧾 سفارش‌های پنل" in menu_labels("reseller_admin")
    assert "🧾 سفارش‌های پنل" not in menu_labels("operator")


def test_the_bot_places_a_validated_order_and_refuses_a_closed_server(client, session, seller):
    placed = telegram_consumer.place_order(STRANGER_ID, seller, "South Sales", 20, 2, "سفارش تلگرامی")
    assert placed["status"] == "pending"
    assert session.scalar(select(Actor).where(Actor.telegram_id == STRANGER_ID)) is not None
    hidden = session.scalar(text("SELECT id FROM panels LIMIT 1"))
    session.execute(text("UPDATE panels SET saleable=false WHERE id=:id"), {"id": hidden}); session.commit()
    with pytest.raises(PermissionError):
        telegram_consumer.place_order(STRANGER_ID, seller, "West Sales", 5, 1, "")
    text_, markup = telegram_consumer.order_panels(STRANGER_ID)
    assert markup is None or not any("🛒" in button["text"] for row in markup["inline_keyboard"] for button in row)


def test_the_bot_walks_the_same_state_machine_as_the_api(client, session, seller):
    placed = telegram_consumer.place_order(CUSTOMER_ID, seller, "North Sales", 50, 3, "")
    queue_text, markup = telegram_consumer.order_queue(ROOT_ID)
    assert placed["business_name"] in queue_text
    assert any(button["callback_data"] == f"o|{placed['id']}|quote"
               for row in markup["inline_keyboard"] for button in row)
    with pytest.raises(PermissionError):
        telegram_consumer.apply_quote(STRANGER_ID, bot_quote_data(placed["id"]))
    quoted = telegram_consumer.apply_quote(ROOT_ID, bot_quote_data(placed["id"]))
    assert quoted["status"] == "quoted"
    detail, buttons = telegram_consumer.order_detail(CUSTOMER_ID, placed["id"])
    assert QUOTE["payment_instructions"] in detail
    assert any(button["callback_data"] == f"o|{placed['id']}|confirm" for row in buttons["inline_keyboard"] for button in row)
    message, row, seller_org = telegram_consumer.customer_order_action(CUSTOMER_ID, placed["id"], "confirm")
    assert row["status"] == "confirmed" and seller_org
    message, row, _ = telegram_consumer.customer_order_action(CUSTOMER_ID, placed["id"], "pay")
    assert row["status"] == "payment_declared"
    result = telegram_consumer.reviewer_order_action(ROOT_ID, placed["id"], "ok")
    assert result["balance_irr"] == QUOTE["amount_irr"]
    assert session.scalar(text("SELECT count(*) FROM ledger_imbalance")) == 0
    assert session.scalar(select(Membership).where(Membership.actor_id == session.scalar(
        select(Actor.id).where(Actor.telegram_id == CUSTOMER_ID)))).role == "customer"
    bound = session.scalar(text("SELECT pg_admin_id FROM bindings WHERE organization_id=:org"),
                           {"org": result["organization_id"]})
    assert int(bound) == QUOTE["pg_admin_id"]
    panel_text, panel_markup = telegram_consumer.customer_panel(CUSTOMER_ID)
    assert "Frankfurt-01" in panel_text and "🌐 پنل کاربری" in markup_labels(panel_markup)


@pytest.mark.parametrize("section", ["funding", "orders", "servers", "approvals"])
def test_an_unlinked_chat_is_refused_every_staff_section(client, session, seller, monkeypatch, section):
    sent = []

    async def record(chat_id, message, markup=None):
        sent.append(message)

    monkeypatch.setattr(telegram_consumer, "send", record)
    asyncio.run(telegram_consumer.open_section(STRANGER_ID, section, None))
    assert sent and "اجازه" in sent[0], (section, sent)


def test_a_customer_can_open_the_order_areas_and_nothing_else(client, session, seller, monkeypatch):
    telegram_consumer.place_order(CUSTOMER_ID, seller, "North Sales", 10, 1, "")
    sent = []

    async def record(chat_id, message, markup=None):
        sent.append(message)

    monkeypatch.setattr(telegram_consumer, "send", record)
    asyncio.run(telegram_consumer.open_section(CUSTOMER_ID, "my_orders", None))
    assert sent and "North Sales" in sent[0]
    sent.clear()
    asyncio.run(telegram_consumer.open_section(CUSTOMER_ID, "resellers", None))
    assert "اجازه" in sent[0]


# --------------------------------------------------------------------------------------
# What an approved order actually opens: the buyer administers subscribers, nothing else
# --------------------------------------------------------------------------------------

class FakeSubscribers:
    """The mother panel answers subscriber calls; the user it reports belongs to ``owner_admin``."""

    def __init__(self, recorded, owner_admin):
        self.recorded = recorded
        self.owner_admin = owner_admin

    def __call__(self, *args, **kwargs):
        return self

    def nodes(self):
        return {"nodes": []}

    def users(self, admin_id, offset=0, limit=50):
        self.recorded.append(("users", admin_id, offset, limit))
        return {"users": [{"id": 9, "username": "subscriber-a", "status": True}], "total": 1}

    def user(self, user_id):
        self.recorded.append(("user", user_id))
        return {"id": user_id, "admin_id": self.owner_admin}

    def set_user_enabled(self, user_id, enabled):
        self.recorded.append(("set_user_enabled", user_id, enabled))
        return {"ok": True}

    def reset_user_data(self, user_id):
        self.recorded.append(("reset_user_data", user_id))
        return {"ok": True}


def fulfill(client, seller, user_id=CUSTOMER_ID):
    """Order, quote, accept, declare payment and approve; returns the approval result."""
    order_id = order(client, seller, user_id).json()["id"]
    client.post(f"/v1/webapp/orders/{order_id}/quote", headers=as_user(ROOT_ID), json=QUOTE)
    client.post(f"/v1/webapp/orders/{order_id}/confirm", headers=as_user(user_id))
    client.post(f"/v1/webapp/orders/{order_id}/payment", headers=as_user(user_id))
    approved = client.post(f"/v1/webapp/orders/{order_id}/approve", headers=as_user(ROOT_ID))
    assert approved.status_code == 200, approved.text
    return approved.json()


def test_the_customer_area_sees_only_its_own_bound_server(client, session, seller, monkeypatch):
    result = fulfill(client, seller)
    monkeypatch.setattr(web_api, "Client", FakeSubscribers([], QUOTE["pg_admin_id"]))
    mine = client.get("/v1/webapp/my-panel", headers=as_user(CUSTOMER_ID))
    assert mine.status_code == 200, mine.text
    body = mine.json()
    assert body["name"] == "Frankfurt-01" and body["panel_id"] == seller
    assert "base_url" not in body and "api_key" not in str(body)
    # The seller's server list is a staff surface: the buyer only ever gets its own binding back.
    assert client.get("/v1/webapp/panels", headers=as_user(CUSTOMER_ID)).status_code == 403
    assert client.get("/v1/webapp/my-panel", headers=as_user(OTHER_MANAGER_ID)).status_code == 403
    assert result["organization_id"]


def test_a_customer_lists_and_operates_the_subscribers_of_its_admin(client, session, seller, monkeypatch):
    fulfill(client, seller)
    calls = []
    monkeypatch.setattr(web_api, "Client", FakeSubscribers(calls, QUOTE["pg_admin_id"]))
    listed = client.get(f"/v1/webapp/panels/{seller}/users", headers=as_user(CUSTOMER_ID))
    assert listed.status_code == 200, listed.text
    assert [u["username"] for u in listed.json()["users"]] == ["subscriber-a"]
    # The listing is scoped to the admin this organization was bound to, never to the whole panel.
    assert calls[0] == ("users", QUOTE["pg_admin_id"], 0, 50)
    disabled = client.post(f"/v1/webapp/panels/{seller}/users/9/disable", headers=as_user(CUSTOMER_ID))
    assert disabled.status_code == 200, disabled.text
    assert ("set_user_enabled", 9, False) in calls
    assert session.scalar(text("SELECT count(*) FROM audit_logs WHERE action='user.disable'")) == 1


@pytest.mark.parametrize("target", ["enable", "reset-data"])
def test_a_subscriber_of_another_admin_is_out_of_reach(client, seller, monkeypatch, target):
    fulfill(client, seller)
    calls = []
    # The panel reports the subscriber belongs to a different admin account.
    monkeypatch.setattr(web_api, "Client", FakeSubscribers(calls, QUOTE["pg_admin_id"] + 1))
    refused = client.post(f"/v1/webapp/panels/{seller}/users/9/{target}", headers=as_user(CUSTOMER_ID))
    assert refused.status_code == 403
    assert refused.json()["detail"] == "این مشترک به پنل شما تعلق ندارد"
    assert not [call for call in calls if call[0] in ("set_user_enabled", "reset_user_data")]


def test_the_seller_still_reaches_every_subscriber_on_its_own_server(client, seller, monkeypatch):
    fulfill(client, seller)
    calls = []
    monkeypatch.setattr(web_api, "Client", FakeSubscribers(calls, QUOTE["pg_admin_id"] + 1))
    assert client.get(f"/v1/webapp/panels/{seller}/users", headers=as_user(ROOT_ID)).status_code == 200
    assert client.post(f"/v1/webapp/panels/{seller}/users/9/reset-data", headers=as_user(ROOT_ID)).status_code == 200
    assert not [call for call in calls if call[0] == "user"], "an owner needs no binding-scoped check"
