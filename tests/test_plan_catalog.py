"""The plan catalog: a published price a customer buys without negotiating, and the guards on it.

The total of a plan is never stored — it is recomputed from rate × volume × duration on every read
— so these tests follow a plan from the seller's shelf to a funded account and assert that a client
can neither invent a price nor reach one that is closed.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import select, text

from conftest import as_user
from control_plane import telegram_consumer, web_api
from control_plane.app import Actor, Membership, Plan, PlanCategory
from control_plane.web_api import OrderApproveIn

ROOT_ID = 100001
CUSTOMER_ID = 900001
STRANGER_ID = 900002
OPERATOR_ID = 300003
API_KEY = "catalog-panel-key"
URL = "https://panel.example.test"
OTHER_URL = "https://panel-2.example.test"
SHELF = {"name": "پلن‌های ماهانه", "description": "بسته‌های ۳۰ روزه"}
PLAN = {"name": "۳۰ روز اقتصادی", "description": "مناسب شروع", "price_per_gib_irr": 25_000, "daily_gib": 3,
        "duration_days": 30, "user_count": 5, "credit_limit_irr": 0,
        "payment_instructions": "کارت 6037-9988-1122-3344 به نام مجموعه مرکزی"}
AMOUNT = PLAN["price_per_gib_irr"] * PLAN["daily_gib"] * PLAN["duration_days"]
# Each lever that hides a plan: the plan itself, the shelf it sits on, or the server it is sold on.
LEVERS = {"plan": ("plans/{id}/status", "status", "inactive"),
          "shelf": ("plan-categories/{id}/status", "status", "inactive"),
          "panel": ("panels/{id}/saleable", "saleable", "false")}


class FakePanel:
    """Panel registration only probes the upstream; the catalog never talks to it."""

    def __call__(self, *args, **kwargs):
        return self

    def nodes(self):
        return {"nodes": []}


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def seller(client, session, monkeypatch):
    """A bootstrapped workspace whose single server is on the customer sale list."""
    monkeypatch.setattr(web_api, "Client", FakePanel())
    client.post("/v1/onboarding/bootstrap", headers=as_user(ROOT_ID),
                json={"business_name": "Central Holdings", "slug": "central-holdings",
                      "setup_token": os.environ["INITIAL_SETUP_TOKEN"]})
    return add_panel(client)


@pytest.fixture
def catalog(client, seller):
    """One published plan sitting on one open shelf."""
    shelf = make_shelf(client)
    return {"panel": seller, "shelf": shelf, "plan": make_plan(client, seller, category_id=shelf)["id"]}


def add_panel(client, url=URL, name="Frankfurt-01"):
    panel = client.post("/v1/webapp/panels", headers=as_user(ROOT_ID),
                        json={"name": name, "base_url": url, "api_key": API_KEY}).json()["id"]
    assert client.post(f"/v1/webapp/panels/{panel}/saleable", params={"saleable": "true"},
                       headers=as_user(ROOT_ID)).status_code == 200
    return panel


def make_shelf(client, user_id=ROOT_ID, **overrides):
    created = client.post("/v1/webapp/plan-categories", headers=as_user(user_id), json={**SHELF, **overrides})
    assert created.status_code == 200, created.text
    return created.json()["id"]


def make_plan(client, panel, user_id=ROOT_ID, **overrides):
    created = client.post("/v1/webapp/plans", headers=as_user(user_id), json={"panel_id": panel, **PLAN, **overrides})
    assert created.status_code == 200, created.text
    return created.json()


def order(client, catalog, user_id=STRANGER_ID, **overrides):
    """Buy the catalog plan, but lie about the size: the plan is the only accepted price source."""
    payload = {"panel_id": catalog["panel"], "plan_id": catalog["plan"], "business_name": "North Sales",
               "user_count": 9999, "daily_gib": 9999, "note": "سفارش پلنی"}
    created = client.post("/v1/webapp/orders", headers=as_user(user_id), json={**payload, **overrides})
    assert created.status_code == 200, created.text
    return created.json()


def pay(client, order_id, user_id=STRANGER_ID):
    assert client.post(f"/v1/webapp/orders/{order_id}/confirm", headers=as_user(user_id)).status_code == 200
    assert client.post(f"/v1/webapp/orders/{order_id}/payment", headers=as_user(user_id)).status_code == 200


def visible_plans(client, user_id=STRANGER_ID):
    options = client.get("/v1/webapp/order-options", headers=as_user(user_id))
    assert options.status_code == 200, options.text
    return options.json()["plans"]


# --------------------------------------------------------------------------------------
# Who may publish, and what a published plan is worth
# --------------------------------------------------------------------------------------

def test_the_catalog_is_closed_to_everyone_but_a_manager(client, session, seller, make_org, make_actor):
    make_actor(OPERATOR_ID, make_org(), "operator")
    for user_id in (OPERATOR_ID, STRANGER_ID):
        assert client.get("/v1/webapp/catalog", headers=as_user(user_id)).status_code == 403
        assert client.post("/v1/webapp/plan-categories", headers=as_user(user_id), json=SHELF).status_code == 403
        assert client.post("/v1/webapp/plans", headers=as_user(user_id),
                           json={"panel_id": seller, **PLAN}).status_code == 403
    assert "کاتالوگ" in client.post("/v1/webapp/plan-categories", headers=as_user(OPERATOR_ID),
                                    json=SHELF).json()["detail"]
    assert client.get("/v1/webapp/catalog", headers=as_user(ROOT_ID)).json() == {"categories": [], "plans": []}
    assert session.scalar(text("SELECT count(*) FROM plans")) == 0


def test_a_plan_carries_its_own_total_instead_of_a_typed_one(client, session, seller):
    row = make_plan(client, seller)
    assert row["amount_irr"] == AMOUNT and row["status"] == "active" and row["panel_name"] == "Frankfurt-01"
    assert "amount_irr" not in Plan.__table__.columns, "a stored total is a total that can disagree"
    shelf = make_shelf(client)
    second = make_plan(client, seller, name="۶۰ روز حرفه‌ای", price_per_gib_irr=50_000, daily_gib=6,
                       duration_days=60, category_id=shelf)
    body = client.get("/v1/webapp/catalog", headers=as_user(ROOT_ID)).json()
    assert [c["plans"] for c in body["categories"]] == [1] and body["categories"][0]["status"] == "active"
    assert [p["amount_irr"] for p in body["plans"]] == [AMOUNT, 50_000 * 6 * 60]
    assert second["category_id"] == shelf
    assert session.scalar(text("SELECT count(*) FROM audit_logs WHERE action='plan.create'")) == 2


def test_two_sellers_never_share_a_shelf_or_a_server(client, session, seller, make_org, make_actor):
    rival = make_org()
    make_actor(OPERATOR_ID, rival, "reseller_admin")
    assert client.get("/v1/webapp/catalog", headers=as_user(OPERATOR_ID)).json() == {"categories": [], "plans": []}
    # A plan may only sit on a server this organization owns, and on a shelf of its own.
    assert client.post("/v1/webapp/plans", headers=as_user(OPERATOR_ID),
                       json={"panel_id": seller, **PLAN}).status_code == 404
    shelf = make_shelf(client, user_id=OPERATOR_ID)
    assert client.post("/v1/webapp/plan-categories", headers=as_user(OPERATOR_ID), json=SHELF).status_code == 409
    assert client.put(f"/v1/webapp/plan-categories/{shelf}", headers=as_user(ROOT_ID), json=SHELF).status_code == 404
    assert session.scalar(select(Plan).where(Plan.organization_id == rival.id)) is None


def test_a_shelf_or_plan_can_be_closed_without_losing_it(client, session, catalog):
    assert client.post(f"/v1/webapp/plans/{catalog['plan']}/status", params={"status": "gone"},
                       headers=as_user(ROOT_ID)).status_code == 422
    assert client.post(f"/v1/webapp/plans/{catalog['plan']}/status", params={"status": "inactive"},
                       headers=as_user(ROOT_ID)).status_code == 200
    plan = session.get(Plan, catalog["plan"])
    assert plan.status == "inactive" and plan.price_per_gib_irr == PLAN["price_per_gib_irr"]
    assert client.delete(f"/v1/webapp/plan-categories/{catalog['shelf']}", headers=as_user(ROOT_ID)).status_code == 409


def test_a_bought_plan_can_never_be_erased(client, session, catalog):
    bought = order(client, catalog)
    pay(client, bought["id"])
    assert client.post(f"/v1/webapp/orders/{bought['id']}/approve", headers=as_user(ROOT_ID),
                       json={"pg_admin_id": 77}).json()["bound"] is True
    refused = client.delete(f"/v1/webapp/plans/{catalog['plan']}", headers=as_user(ROOT_ID))
    assert refused.status_code == 409 and "بست" in refused.json()["detail"]
    assert session.scalar(text("SELECT count(*) FROM panel_orders")) == 1
    assert session.scalar(text("SELECT count(*) FROM ledger_imbalance")) == 0


# --------------------------------------------------------------------------------------
# What a customer is shown, and what a customer is charged
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("lever", ["plan", "shelf", "panel"])
def test_a_plan_is_buyable_only_while_every_part_of_it_is_open(client, seller, catalog, lever):
    assert [p["id"] for p in visible_plans(client)] == [catalog["plan"]]
    route, key, closed = LEVERS[lever]
    target = catalog[lever]
    assert client.post(f"/v1/webapp/{route.format(id=target)}", params={key: closed},
                       headers=as_user(ROOT_ID)).status_code == 200
    assert visible_plans(client) == []
    refused = client.post("/v1/webapp/orders", headers=as_user(STRANGER_ID),
                          json={"panel_id": catalog["panel"], "plan_id": catalog["plan"],
                                "business_name": "North Sales", "user_count": 1, "daily_gib": 1, "note": ""})
    assert refused.status_code == 422 and "باز نیست" in refused.json()["detail"]
    assert client.post(f"/v1/webapp/{route.format(id=target)}",
                       params={key: "true" if lever == "panel" else "active"},
                       headers=as_user(ROOT_ID)).status_code == 200
    assert [p["id"] for p in visible_plans(client)] == [catalog["plan"]]


def test_a_plan_prices_the_order_and_the_browser_says_nothing(client, session, catalog):
    bought = order(client, catalog)
    assert bought["status"] == "quoted", "a published price needs no negotiation"
    assert bought["user_count"] == PLAN["user_count"] and bought["daily_gib"] == PLAN["daily_gib"]
    assert bought["amount_irr"] == AMOUNT and bought["price_per_gib_irr"] == PLAN["price_per_gib_irr"]
    assert bought["credit_limit_irr"] == AMOUNT, "an unpinned credit falls back to the plan amount"
    assert bought["plan_name"] == PLAN["name"] and bought["payment_instructions"] == PLAN["payment_instructions"]
    assert session.scalar(text("SELECT count(*) FROM transactions")) == 0
    # Skipping the manual quote is the point, so the quote route must refuse rather than reprice.
    assert client.post(f"/v1/webapp/orders/{bought['id']}/quote", headers=as_user(ROOT_ID),
                       json={"amount_irr": 1, "credit_limit_irr": 0, "price_per_gib_irr": 1,
                             "payment_instructions": "کارت 6037"}).status_code == 409
    # A hand-priced order still works exactly as before, side by side with the catalog.
    manual = client.post("/v1/webapp/orders", headers=as_user(CUSTOMER_ID),
                         json={"panel_id": catalog["panel"], "business_name": "South Sales",
                               "user_count": 20, "daily_gib": 2, "note": ""})
    assert manual.json()["status"] == "pending" and manual.json()["plan_id"] is None


def test_a_plan_cannot_be_moved_onto_another_server(client, session, seller, catalog):
    other = add_panel(client, url=OTHER_URL, name="Amsterdam-02")
    refused = client.post("/v1/webapp/orders", headers=as_user(STRANGER_ID),
                          json={"panel_id": other, "plan_id": catalog["plan"], "business_name": "North Sales",
                                "user_count": 5, "daily_gib": 3, "note": ""})
    assert refused.status_code == 422 and "این سرور" in refused.json()["detail"]
    moved = client.put(f"/v1/webapp/plans/{catalog['plan']}", headers=as_user(ROOT_ID),
                       json={"panel_id": other, "category_id": catalog["shelf"], **PLAN})
    assert moved.json()["panel_name"] == "Amsterdam-02"
    assert session.scalar(text("SELECT count(*) FROM panel_orders")) == 0


def test_repricing_a_plan_changes_the_next_sale_not_the_last_one(client, session, catalog):
    bought = order(client, catalog)
    raised = client.put(f"/v1/webapp/plans/{catalog['plan']}", headers=as_user(ROOT_ID),
                        json={"panel_id": catalog["panel"], "category_id": catalog["shelf"],
                              **{**PLAN, "price_per_gib_irr": 40_000}})
    assert raised.json()["amount_irr"] == 40_000 * PLAN["daily_gib"] * PLAN["duration_days"]
    assert client.get("/v1/webapp/orders", headers=as_user(STRANGER_ID)).json()[0]["amount_irr"] == AMOUNT
    assert visible_plans(client)[0]["amount_irr"] == raised.json()["amount_irr"]


def test_a_bought_plan_becomes_a_funded_and_bound_customer(client, session, catalog):
    bought = order(client, catalog)
    pay(client, bought["id"])
    approved = client.post(f"/v1/webapp/orders/{bought['id']}/approve", headers=as_user(ROOT_ID),
                           json={"pg_admin_id": 77, "admin_username": "north-admin"})
    assert approved.status_code == 200, approved.text
    result = approved.json()
    assert result["balance_irr"] == AMOUNT and result["bound"] is True
    assert session.scalar(text("SELECT count(*) FROM ledger_imbalance")) == 0
    organization = result["organization_id"]
    assert session.scalar(text("SELECT credit_limit_irr FROM organizations WHERE id=:id"),
                          {"id": organization}) == AMOUNT
    assert session.scalar(text("SELECT pg_admin_id FROM bindings WHERE organization_id=:org"),
                          {"org": organization}) == 77
    customer = session.scalar(select(Actor.id).where(Actor.telegram_id == STRANGER_ID))
    assert session.scalar(select(Membership).where(Membership.actor_id == customer)).role == "customer"


# --------------------------------------------------------------------------------------
# The same catalog through the Telegram bot
# --------------------------------------------------------------------------------------

def menu_labels(role):
    return [button["text"] for row in telegram_consumer.menu_for(role)["keyboard"] for button in row]


def callback_data(markup):
    return [button["callback_data"] for row in (markup or {}).get("inline_keyboard", []) for button in row]


@pytest.fixture
def chat(monkeypatch):
    """A bot conversation with no HTTP: the messages it would send are collected instead."""
    sent = []

    async def record(chat_id, message, markup=None):
        sent.append((chat_id, message, markup))

    async def answered(callback_id, message="انجام شد"):
        pass

    monkeypatch.setattr(telegram_consumer, "send", record)
    monkeypatch.setattr(telegram_consumer, "answer_callback", answered)
    return FakeRedis(), sent


def test_the_catalog_menu_follows_the_same_permission_matrix():
    assert "🛍 کاتالوگ پلن" in menu_labels("system_admin") and "🛍 کاتالوگ پلن" in menu_labels("reseller_admin")
    for role in ("operator", "finance", "viewer", "customer"):
        assert "catalog" not in telegram_consumer.sections_for(role), role
        assert "🛍 کاتالوگ پلن" not in menu_labels(role), role


def test_a_chat_that_may_not_price_cannot_write_the_catalog(client, session, seller, make_org, make_actor):
    rival = make_actor(OPERATOR_ID, make_org(), "reseller_admin")
    with pytest.raises(PermissionError):
        telegram_consumer.catalog_list(STRANGER_ID)
    with pytest.raises(PermissionError):
        telegram_consumer.create_shelf(STRANGER_ID, {"name": "دسته‌ای از بیرون"})
    with pytest.raises(PermissionError):
        telegram_consumer.save_plan(STRANGER_ID, {"panel_id": seller, **PLAN})
    # A manager may price its own shelves, so another seller's plan is simply not in this catalog.
    shelf = make_shelf(client)
    with pytest.raises(PermissionError):
        telegram_consumer.toggle_catalog(OPERATOR_ID, "category", shelf)
    assert telegram_consumer.context(rival.telegram_id)["role"] == "reseller_admin"


def test_the_bot_publishes_a_plan_by_answering_questions(client, session, seller, chat):
    redis, sent = chat
    asyncio.run(telegram_consumer.handle_callback(redis, ROOT_ID, "cb", "catnew"))
    asyncio.run(telegram_consumer.handle_message(redis, ROOT_ID, SHELF["name"]))
    asyncio.run(telegram_consumer.handle_message(redis, ROOT_ID, "بعد"))
    assert asyncio.run(telegram_consumer.get_state(redis, ROOT_ID)) is None
    shelf = session.scalar(select(PlanCategory).where(PlanCategory.name == SHELF["name"]))
    assert shelf.status == "active"

    asyncio.run(telegram_consumer.handle_callback(redis, ROOT_ID, "cb", "plnew"))
    asyncio.run(telegram_consumer.handle_callback(redis, ROOT_ID, "cb", f"plsel|{seller}"))
    button = next(b for b in callback_data(sent[-1][2]) if b.startswith("plsh|"))
    assert button.endswith(shelf.id), "the picker offers the seller's own shelves"
    asyncio.run(telegram_consumer.handle_callback(redis, ROOT_ID, "cb", button))
    for answer in (PLAN["name"], "۲۵,۰۰۰", "3", "30", "5", "خودکار", PLAN["payment_instructions"]):
        asyncio.run(telegram_consumer.handle_message(redis, ROOT_ID, answer))
    assert any("ساخته شد" in message for _chat, message, _markup in sent)
    assert asyncio.run(telegram_consumer.get_state(redis, ROOT_ID)) is None

    plan = session.scalar(select(Plan))
    assert plan.amount_irr == AMOUNT and plan.category_id == shelf.id and plan.panel_id == seller
    assert plan.credit_limit_irr == 0, "«خودکار» means the plan total, which the order computes"
    assert [p["id"] for p in visible_plans(client)] == [plan.id]


def test_a_bought_plan_never_asks_the_chat_for_a_price(client, session, seller, catalog):
    message, markup = telegram_consumer.order_panels(STRANGER_ID)
    buttons = callback_data(markup)
    assert PLAN["name"] in message
    assert buttons.index(f"oplan|{catalog['plan']}") < buttons.index(f"onew|{seller}"), "plans come first"

    bought = telegram_consumer.place_order(CUSTOMER_ID, seller, "North Sales", 1, 1, "", plan_id=catalog["plan"])
    assert bought["status"] == "quoted" and bought["amount_irr"] == AMOUNT
    assert telegram_consumer.pick_plan(catalog["plan"])["amount_irr"] == AMOUNT

    assert telegram_consumer.customer_order_action(CUSTOMER_ID, bought["id"], "confirm")[1]["status"] == "confirmed"
    _, declared, seller_org = telegram_consumer.customer_order_action(CUSTOMER_ID, bought["id"], "pay")
    assert declared["status"] == "payment_declared" and seller_org
    # The admin account is the one thing a published plan cannot say, so approval asks for it.
    assert telegram_consumer.order_needs_admin(bought["id"]) is True
    result = telegram_consumer.reviewer_order_action(ROOT_ID, bought["id"], "ok",
                                                     attach=OrderApproveIn(pg_admin_id=91))
    assert result["balance_irr"] == AMOUNT and result["bound"] is True
    assert session.scalar(text("SELECT count(*) FROM ledger_imbalance")) == 0
    assert int(session.scalar(text("SELECT pg_admin_id FROM bindings WHERE organization_id=:org"),
                             {"org": result["organization_id"]})) == 91

    assert "بسته شد" in telegram_consumer.toggle_catalog(ROOT_ID, "plan", catalog["plan"])
    assert telegram_consumer.pick_plan(catalog["plan"]) is None
    assert telegram_consumer.toggle_catalog(ROOT_ID, "plan", catalog["plan"]).startswith("پلن برای فروش باز شد")
    with pytest.raises(PermissionError):
        telegram_consumer.place_order(STRANGER_ID, seller, "West Sales", 1, 1, "", plan_id="not-a-plan")
