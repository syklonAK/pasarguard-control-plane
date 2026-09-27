"""The Telegram surface: a customer menu for ordering a panel and a management menu for staff.

A stranger is never dead-ended. Without a membership the bot resolves the chat to the customer
role, which can place and follow an order; every staff section stays behind the same permission
matrix the WebApp API enforces, so no keyboard here grants anything the core would refuse.
"""
import asyncio
import html
import json
import os
import re

import httpx
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import func, select, text

from .app import (
    Account, Actor, ApprovalRequest, Binding, Checkpoint, Closure, Contract, Entry, FundingRequest,
    Membership, Organization, Panel, PanelOrder, PanelOwner, SessionLocal, Transaction, account, audit,
    effective_role, now, transfer,
)
from .rbac import can, denial_fa, role_fa
from .web_api import (
    OrderQuoteIn, _unwrap, advance_customer_order, approve_panel_order, create_panel_order, locked_order,
    order_row, panel_operation, panel_saleable_rows, quote_panel_order, reject_panel_order, requester_actor,
    review_panels,
)

STREAM = "telegram_updates"
GROUP = "telegram-workers"
STATE_TTL = 900
NL = chr(10)
STAFF, CUSTOMER, BOTH = "staff", "customer", "both"

# One table drives both keyboards: a section is offered only when the resolved role holds the
# permission, so the bot never shows an action the API would refuse. TOP keeps the reply keyboard
# short — a sub-section is reached from the area that owns it.
SECTIONS = (
    ("dashboard", "📊 داشبورد", "view_dashboard", STAFF),
    ("orders", "🧾 سفارش‌های پنل", "decide_orders", STAFF),
    ("servers", "🖥 سرورها", "manage_servers", STAFF),
    ("resellers", "👥 نمایندگان", "manage_resellers", STAFF),
    ("finance", "💰 مرکز مالی", "view_finance", STAFF),
    ("approvals", "✅ تأیید اصلاحات", "decide_adjustment", STAFF),
    ("system", "⚙️ مدیریت سیستم", "view_audit", STAFF),
    ("webapp", "🌐 وب‌اپ", "view_dashboard", STAFF),
    ("transactions", "📜 تراکنش‌ها", "view_finance", STAFF),
    ("funding", "💴 درخواست‌های مالی", "decide_funding", STAFF),
    ("credit", "➕ درخواست اعتبار", "request_funding", STAFF),
    ("new_reseller", "➕ نماینده جدید", "manage_resellers", STAFF),
    ("new_order", "➕ سفارش پنل", "order_panel", CUSTOMER),
    ("my_orders", "🧾 سفارش‌های من", "order_panel", CUSTOMER),
    ("my_panel", "🖥 پنل و مشترکان", "view_dashboard", CUSTOMER),
    ("usage", "📈 مصرف و تراکنش", "view_finance", CUSTOMER),
    ("topup", "💳 درخواست شارژ", "request_funding", CUSTOMER),
    ("my_webapp", "🌐 پنل کاربری", "view_dashboard", CUSTOMER),
    ("support", "🛟 پشتیبانی", "create_support_ticket", BOTH),
)
TOP = {STAFF: ("dashboard", "orders", "servers", "resellers", "finance", "approvals", "system", "support", "webapp"),
       CUSTOMER: ("new_order", "my_orders", "my_panel", "usage", "topup", "support", "my_webapp")}
LABELS = {name: label for name, label, _permission, _audience in SECTIONS}
SECTION_PERMISSION = {name: permission for name, _label, permission, _audience in SECTIONS}
ALIASES = {"/dashboard": "dashboard", "/servers": "servers", "/support": "support", "/resellers": "resellers",
           "/finance": "finance", "/orders": "orders", "/menu": "menu"}


def audience_for(role: str) -> str:
    return CUSTOMER if role == CUSTOMER else STAFF


def sections_for(role):
    """Every section this role may open, staff and customer kept apart."""
    audience = audience_for(role)
    return [name for name, _, permission, section_audience in SECTIONS
            if section_audience in (audience, BOTH) and can(role, permission)]


def top_for(role):
    return [name for name in TOP[audience_for(role)] if name in sections_for(role)]


def menu_for(role):
    """The persistent reply keyboard: the areas only, never the sub-sections."""
    rows = [LABELS[name] for name in top_for(role)]
    lines = [rows[i:i + 2] for i in range(0, len(rows), 2)]
    return {"keyboard": [[{"text": label} for label in line] for line in lines],
            "resize_keyboard": True, "is_persistent": True}


def section_menu(role):
    names = sections_for(role)
    rows = [names[i:i + 2] for i in range(0, len(names), 2)]
    return {"inline_keyboard": [[{"text": LABELS[name], "callback_data": f"section|{name}"} for name in row]
                                for row in rows] + [[{"text": "🔙 منوی اصلی", "callback_data": "section|menu"}]]}


def back_button(label="🔙 منو", target="menu"):
    return {"inline_keyboard": [[{"text": label, "callback_data": f"section|{target}"}]]}


def label_to_section(label):
    for name, item_label, _permission, _audience in SECTIONS:
        if item_label == label:
            return name
    return None


def panel_button(label="ورود به پنل"):
    url = os.getenv("WEBAPP_URL", "")
    return {"inline_keyboard": [[{"text": label, "web_app": {"url": url}}]]} if url else None


async def telegram(method, payload):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = "https:" + "//" + "api.telegram.org" + "/bot" + token + "/" + method
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, json=payload); response.raise_for_status(); return response.json()


async def send(chat_id, message, reply_markup=None):
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    if reply_markup: payload["reply_markup"] = reply_markup
    await telegram("sendMessage", payload)


async def answer_callback(callback_id, message="انجام شد"):
    await telegram("answerCallbackQuery", {"callback_query_id": callback_id, "text": message})


def context(chat_id):
    with SessionLocal() as session:
        actor = session.scalar(select(Actor).where(Actor.telegram_id == chat_id, Actor.status == "active"))
        if not actor: return None
        membership = session.scalar(select(Membership).where(Membership.actor_id == actor.id, Membership.status == "active"))
        if not membership: return None
        organization = session.get(Organization, membership.organization_id)
        return {"actor_id": actor.id, "organization_id": organization.id, "organization_name": organization.name,
                "role": effective_role(chat_id, membership.role)}


def require_context(chat_id):
    value = context(chat_id)
    if not value: raise PermissionError("برای این بخش نیاز به حساب متصل دارید؛ سفارش خود را ثبت کنید تا حساب ساخته شود.")
    return value


def money(value): return f"{int(value or 0):,} ریال"


def users(count): return f"{int(count or 0):,} کاربر"


def gib(value): return f"{int(value or 0):,} گیگابایت در روز"


def dashboard(chat_id, admin=False):
    ctx = require_context(chat_id)
    with SessionLocal() as session:
        organization = session.get(Organization, ctx["organization_id"])
        wallet = session.scalar(select(Account).where(Account.owner_key == organization.id, Account.code == "wallet"))
        binding = session.scalar(select(Binding).where(Binding.organization_id == organization.id))
        checkpoint = session.get(Checkpoint, binding.id) if binding else None
        direct_children = int(session.scalar(select(func.count()).select_from(Organization).where(Organization.parent_id == organization.id)) or 0)
        if admin:
            organizations = int(session.scalar(select(func.count()).select_from(Organization)) or 0)
            panels = int(session.scalar(select(func.count()).select_from(Panel).where(Panel.status == "active")) or 0)
            pending = int(session.scalar(select(func.count()).select_from(FundingRequest).where(FundingRequest.status == "pending")) or 0)
            open_orders = int(session.scalar(select(func.count()).select_from(PanelOrder).where(PanelOrder.status == "pending")) or 0)
        else: organizations = panels = pending = open_orders = 0
    wallet_balance = int(wallet.balance_irr if wallet else 0); credit = int(organization.credit_limit_irr)
    usage = int(checkpoint.lifetime_bytes if checkpoint else 0) / 1073741824
    title = "داشبورد مدیریت" if admin else html.escape(organization.name)
    lines = [f"<b>{title}</b>", "", f"موجودی: <b>{money(wallet_balance)}</b>", f"اعتبار: <b>{money(credit)}</b>",
             f"قابل استفاده: <b>{money(wallet_balance + credit)}</b>", f"مصرف: <b>{usage:,.2f} GiB</b>",
             f"زیرمجموعه مستقیم: <b>{direct_children}</b>"]
    if admin:
        lines += ["", f"کل سازمان‌ها: <b>{organizations}</b>", f"سرورهای فعال: <b>{panels}</b>",
                  f"درخواست مالی باز: <b>{pending}</b>", f"سفارش پنل باز: <b>{open_orders}</b>"]
    return NL.join(lines)


def server_list(chat_id):
    ctx = require_context(chat_id)
    with SessionLocal() as session:
        rows = session.execute(text("SELECT p.id,p.name,p.base_url,p.status,p.saleable FROM panels p "
                                    "JOIN panel_owners po ON po.panel_id=p.id WHERE po.organization_id=:org "
                                    "ORDER BY p.name LIMIT 20"), {"org": ctx["organization_id"]}).mappings().all()
    if not rows: return "هنوز سروری ثبت نشده است.", panel_button("ثبت اولین سرور")
    lines = ["<b>سرورها</b>", "<i>سروری که «آماده فروش» باشد در لیست سفارش مشتریان تلگرام قرار می‌گیرد.</i>"]
    keyboard = []
    for row in rows:
        icon = "🟢" if row["status"] == "active" else "🔴"
        sale = "🛒 آماده فروش" if row["saleable"] else "⛔ خارج از فروش"
        lines.append(f"{icon} <b>{html.escape(row['name'])}</b> — {sale}{NL}<code>{html.escape(row['base_url'])}</code>")
        keyboard.append([{"text": f"نودهای {row['name'][:20]}", "callback_data": f"nodes|{row['id']}"}])
        keyboard.append([{"text": "⛔ خروج از فروش" if row["saleable"] else "🛒 ورود به فروش",
                          "callback_data": f"sale|{row['id']}|{0 if row['saleable'] else 1}"}])
    keyboard.append([{"text": "➕ ثبت سرور جدید", "web_app": {"url": os.getenv("WEBAPP_URL", "")}}])
    keyboard.append([{"text": "🔙 منو", "callback_data": "section|menu"}])
    return NL.join(lines), {"inline_keyboard": keyboard}


def set_saleable(chat_id, panel_id, saleable):
    """Only the organization that owns a server can put it on the customer order list."""
    ctx = require_context(chat_id)
    if not can(ctx["role"], "manage_servers"): raise PermissionError(denial_fa("manage_servers"))
    with SessionLocal() as session:
        link = session.scalar(select(PanelOwner).where(PanelOwner.panel_id == panel_id,
                                                       PanelOwner.organization_id == ctx["organization_id"]))
        panel = session.get(Panel, panel_id) if link else None
        if not panel: raise PermissionError("سرور پیدا نشد یا متعلق به شما نیست.")
        panel.saleable = bool(saleable)
        audit(session, "panel.saleable", "panel", panel.id, actor_id=ctx["actor_id"],
              organization_id=ctx["organization_id"], metadata={"saleable": panel.saleable})
        session.commit()
        return "این سرور برای سفارش مشتریان باز شد." if panel.saleable else "سرور از لیست فروش خارج شد."


def bot_panel(chat_id, panel_id):
    """Resolve a panel the caller owns and hand back the open session that loaded it."""
    ctx = require_context(chat_id)
    session = SessionLocal()
    link = session.scalar(select(PanelOwner).where(PanelOwner.panel_id == panel_id,
                                                   PanelOwner.organization_id == ctx["organization_id"]))
    panel = session.get(Panel, panel_id) if link else None
    if not panel or panel.status != "active":
        session.close()
        raise PermissionError("سرور پیدا نشد یا غیرفعال است.")
    return session, ctx, panel


def run_panel_operation(session, ctx, panel, action, run, metadata=None):
    """Share one audited code path with the WebApp and turn its errors into Persian replies."""
    try:
        return panel_operation(session, panel, ctx["organization_id"], action, run, ctx["actor_id"], metadata)
    except HTTPException as exc:
        raise PermissionError(str(exc.detail)) from exc


def node_list(chat_id, panel_id):
    session, ctx, panel = bot_panel(chat_id, panel_id)
    try:
        nodes = _unwrap(run_panel_operation(session, ctx, panel, "panel.nodes.read", lambda c: c.nodes()), "nodes")
    finally:
        session.close()
    lines = [f"<b>نودهای {html.escape(panel.name)}</b>"]; keyboard = []
    for node in nodes[:20]:
        node_id = node.get("id")
        if node_id is None: continue
        name = str(node.get("name") or f"Node {node_id}")
        enabled = node.get("enable", node.get("status"))
        on = enabled in (True, "online", "active")
        lines.append(f"{'🟢' if on else '⚪'} • {html.escape(name)} — <code>{html.escape(str(node.get('message') or node.get('status') or ''))}</code>")
        keyboard.append([{"text": f"🔄 {name[:22]}", "callback_data": f"node|{panel_id}|{node_id}|reconnect"},
                         {"text": ("⏸ " if on else "▶️ ") + name[:20],
                          "callback_data": f"node|{panel_id}|{node_id}|{'disable' if on else 'enable'}"}])
        keyboard.append([{"text": f"♻️ بازنشانی {name[:22]}", "callback_data": f"node|{panel_id}|{node_id}|reset"},
                         {"text": f"📊 وضعیت {name[:22]}", "callback_data": f"node|{panel_id}|{node_id}|status"}])
    if not keyboard: lines.append("نودی دریافت نشد.")
    keyboard.append([{"text": "🔙 سرورها", "callback_data": "section|servers"}])
    return NL.join(lines), {"inline_keyboard": keyboard}


NODE_OPERATIONS = {
    "enable": ("node.enable", lambda i, c: c.set_node_enabled(i, True), "نود روشن شد."),
    "disable": ("node.disable", lambda i, c: c.set_node_enabled(i, False), "نود خاموش شد."),
    "reconnect": ("node.reconnect", lambda i, c: c.reconnect_node(i), "دستور اتصال مجدد ارسال شد."),
    "reset": ("node.reset", lambda i, c: c.reset_node(i), "دستور بازنشانی نود ارسال شد."),
}


def node_action(chat_id, panel_id, node_id, action):
    """Runs one node operation and returns the Persian confirmation, or the status text."""
    if action == "status":
        session, ctx, panel = bot_panel(chat_id, panel_id)
        try:
            status = run_panel_operation(session, ctx, panel, "node.status.read", lambda c: c.node_status(int(node_id)), {"node_id": int(node_id)})
        finally:
            session.close()
        return html.escape(json.dumps(status, ensure_ascii=False)[:900])
    if action not in NODE_OPERATIONS: raise PermissionError("عملیات نامعتبر است.")
    audit_action, call, reply = NODE_OPERATIONS[action]
    session, ctx, panel = bot_panel(chat_id, panel_id)
    try:
        run_panel_operation(session, ctx, panel, audit_action, lambda c: call(int(node_id), c), {"node_id": int(node_id)})
    finally:
        session.close()
    return reply


def reseller_list(chat_id):
    ctx = require_context(chat_id)
    with SessionLocal() as session:
        rows = session.scalars(select(Organization).where(Organization.parent_id == ctx["organization_id"])
                               .order_by(Organization.created_at.desc()).limit(30)).all()
        output = []
        for organization in rows:
            wallet = session.scalar(select(Account).where(Account.owner_key == organization.id, Account.code == "wallet"))
            actor = session.scalar(select(Actor).join(Membership, Membership.actor_id == Actor.id)
                                   .where(Membership.organization_id == organization.id))
            output.append((organization, int(wallet.balance_irr if wallet else 0), actor.telegram_id if actor else None))
    keyboard = [[{"text": "➕ نماینده جدید", "callback_data": "section|new_reseller"},
                 {"text": "💴 درخواست‌های مالی", "callback_data": "section|funding"}],
                [{"text": "🔙 منو", "callback_data": "section|menu"}]]
    if not output: return "هنوز زیرمجموعه‌ای ایجاد نشده است.", {"inline_keyboard": keyboard}
    lines = ["<b>نمایندگان</b>"]
    for organization, credit_used, telegram_id in output:
        linked = f" — <code>{telegram_id}</code>" if telegram_id else " — بدون اتصال تلگرام"
        lines.append(f"• <b>{html.escape(organization.name)}</b> — موجودی {money(credit_used)}{linked}")
    return NL.join(lines), {"inline_keyboard": keyboard}


def transactions(chat_id):
    ctx = require_context(chat_id)
    with SessionLocal() as session:
        wallet = session.scalar(select(Account).where(Account.owner_key == ctx["organization_id"], Account.code == "wallet"))
        if not wallet: return "هنوز تراکنشی ثبت نشده است."
        rows = session.execute(select(Transaction, Entry).join(Entry, Entry.transaction_id == Transaction.id)
                               .where(Entry.account_id == wallet.id).order_by(Transaction.created_at.desc()).limit(15)).all()
    if not rows: return "هنوز تراکنشی ثبت نشده است."
    lines = ["<b>آخرین تراکنش‌ها</b>"]
    for transaction, entry in rows:
        sign = "+" if entry.side == "credit" else "-"
        lines.append(f"{sign}{money(entry.amount_irr)} — {html.escape(transaction.kind)}")
    return NL.join(lines)


def finance_menu(chat_id):
    ctx = require_context(chat_id); role = ctx["role"]; rows = []
    if can(role, "view_finance"): rows.append([{"text": "📜 تراکنش‌ها", "callback_data": "section|transactions"}])
    if can(role, "decide_funding"): rows.append([{"text": "💴 درخواست‌های مالی", "callback_data": "section|funding"}])
    if can(role, "request_funding"): rows.append([{"text": "➕ درخواست اعتبار", "callback_data": "section|credit"}])
    rows.append([{"text": "🔙 منو", "callback_data": "section|menu"}])
    return "<b>مرکز مالی</b> — یک گزینه را انتخاب کنید.", {"inline_keyboard": rows}


def funding_requests(chat_id):
    ctx = require_context(chat_id)
    if not can(ctx["role"], "decide_funding"): raise PermissionError(denial_fa("decide_funding"))
    with SessionLocal() as session:
        rows = session.execute(select(FundingRequest, Organization).join(Organization, Organization.id == FundingRequest.organization_id)
                               .where(FundingRequest.status == "pending", Organization.parent_id == ctx["organization_id"])
                               .order_by(FundingRequest.created_at).limit(20)).all()
    if not rows: return "درخواست مالی بازی وجود ندارد.", None
    lines = ["<b>درخواست‌های افزایش اعتبار زیرمجموعه‌های شما</b>"]; keyboard = []
    for request, organization in rows:
        lines.append(f"• {html.escape(organization.name)} — <b>{money(request.amount_irr)}</b>")
        keyboard.append([{"text": f"✅ تایید {organization.name[:18]}", "callback_data": f"fundok|{request.id}"},
                         {"text": "❌ رد", "callback_data": f"fundno|{request.id}"}])
    keyboard.append([{"text": "🔙 مرکز مالی", "callback_data": "section|finance"}])
    return NL.join(lines), {"inline_keyboard": keyboard}


def decide_funding(chat_id, request_id, approve):
    """The direct parent releases the credit; no other branch can spend this money."""
    ctx = require_context(chat_id)
    if not can(ctx["role"], "decide_funding"): raise PermissionError(denial_fa("decide_funding"))
    with SessionLocal() as session:
        request = session.scalar(select(FundingRequest).where(FundingRequest.id == request_id).with_for_update())
        if not request or request.status != "pending": return "این درخواست قبلاً بررسی شده است.", None
        child = session.get(Organization, request.organization_id)
        if not child or child.parent_id != ctx["organization_id"]: raise PermissionError("این درخواست به شاخهٔ دیگری تعلق دارد.")
        if approve:
            transfer(session, "SYSTEM", request.organization_id, request.amount_irr, f"funding-request:{request.id}",
                     "fund", request.id, False, ctx["actor_id"])
            request.status = "approved"
        else: request.status = "rejected"
        request.decided_by = ctx["actor_id"]; request.decided_at = now()
        audit(session, f"funding_request.{'approve' if approve else 'reject'}", "funding_request", request.id,
              actor_id=ctx["actor_id"], organization_id=request.organization_id, metadata={"amount_irr": request.amount_irr})
        session.commit()
        reply = "درخواست تایید و کیف پول شارژ شد." if approve else "درخواست رد شد."
        actor = session.get(Actor, request.actor_id)
        return reply, actor.telegram_id if actor else None


def approval_requests(chat_id):
    """Wallet corrections wait here for the only role allowed to approve them."""
    ctx = require_context(chat_id)
    if not can(ctx["role"], "decide_adjustment"): raise PermissionError(denial_fa("decide_adjustment"))
    with SessionLocal() as session:
        rows = session.scalars(select(ApprovalRequest).where(ApprovalRequest.status == "pending")
                               .order_by(ApprovalRequest.created_at).limit(20)).all()
        names = {organization.id: organization.name for organization in session.scalars(select(Organization)).all()}
    if not rows: return "درخواست تأیید بازی وجود ندارد.", None
    lines = ["<b>اصلاح حساب‌های در انتظار تأیید</b>"]; keyboard = []
    for approval in rows:
        amount = int(approval.payload["amount_irr"])
        lines.append(f"• {html.escape(names.get(approval.target_id, approval.target_id))} — <b>{money(amount)}</b>"
                     f"{NL}<i>{html.escape(str(approval.payload['note'])[:160])}</i>")
        keyboard.append([{"text": "✅ تأیید", "callback_data": f"appr|{approval.id}|ok"},
                         {"text": "❌ رد", "callback_data": f"appr|{approval.id}|no"}])
    keyboard.append([{"text": "🔙 منو", "callback_data": "section|menu"}])
    return NL.join(lines), {"inline_keyboard": keyboard}


def decide_approval(chat_id, approval_id, approve):
    ctx = require_context(chat_id)
    if not can(ctx["role"], "decide_adjustment"): raise PermissionError(denial_fa("decide_adjustment"))
    from .web_api import APPROVAL_APPLICATORS, locked_pending_approval
    from .outbox import enqueue
    with SessionLocal() as session:
        approval = locked_pending_approval(session, approval_id, now())
        if approve:
            applicator = APPROVAL_APPLICATORS.get(approval.action)
            if not applicator: raise PermissionError("این نوع درخواست قابل اجرا نیست.")
            applicator(session, approval, ctx["actor_id"])
            approval.status = "executed"
        else: approval.status = "rejected"
        approval.approver_id = ctx["actor_id"]; approval.decided_at = now()
        audit(session, f"approval.{'approve' if approve else 'reject'}", "approval_request", approval.id,
              actor_id=ctx["actor_id"], organization_id=ctx["organization_id"],
              metadata={"action": approval.action, "target_id": approval.target_id})
        enqueue(session, "approval.granted", approval.id, {"action": approval.action, "status": approval.status})
        session.commit()
    return "اصلاح حساب اعمال شد." if approve else "درخواست رد شد."


def create_reseller(chat_id, data):
    ctx = require_context(chat_id)
    if not can(ctx["role"], "manage_resellers"): raise PermissionError(denial_fa("manage_resellers"))
    with SessionLocal() as session:
        if session.scalar(select(Organization.id).where(Organization.slug == data["slug"])):
            raise ValueError("این شناسه قبلاً استفاده شده است.")
        child = Organization(name=data["name"], slug=data["slug"], parent_id=ctx["organization_id"],
                             credit_limit_irr=int(data["credit"]))
        session.add(child); session.flush()
        session.add(Closure(ancestor_id=child.id, descendant_id=child.id, depth=0))
        for edge in session.scalars(select(Closure).where(Closure.descendant_id == ctx["organization_id"])).all():
            session.add(Closure(ancestor_id=edge.ancestor_id, descendant_id=child.id, depth=edge.depth + 1))
        session.add(Contract(parent_id=ctx["organization_id"], child_id=child.id, price_per_gib_irr=int(data["price"])))
        account(session, child.id)
        actor = session.scalar(select(Actor).where(Actor.telegram_id == int(data["telegram_id"])))
        if not actor:
            actor = Actor(telegram_id=int(data["telegram_id"]), display_name=data["name"]); session.add(actor); session.flush()
        session.add(Membership(organization_id=child.id, actor_id=actor.id, role="reseller_admin"))
        session.commit()
        return child.name


# --------------------------------------------------------------------------------------
# The panel order funnel
# --------------------------------------------------------------------------------------

STATUS_FA = {"pending": "در انتظار بررسی مدیر", "quoted": "قیمت‌گذاری شد، منتظر تأیید شما",
             "confirmed": "تأیید شد، منتظر پرداخت", "payment_declared": "پرداخت اعلام شد، منتظر تأیید نهایی",
             "approved": "حساب ساخته شد", "rejected": "رد شد", "cancelled": "لغو شد"}


def customer_order_rows(chat_id, limit=8):
    with SessionLocal() as session:
        actor = requester_actor(session, chat_id)
        if not actor: return []
        rows = session.scalars(select(PanelOrder).where(PanelOrder.actor_id == actor.id)
                               .order_by(PanelOrder.created_at.desc()).limit(limit)).all()
        return [order_row(session, order) for order in rows]


def order_panels(chat_id):
    """Servers the customer may order from; the owning admin decides which ones are on sale."""
    with SessionLocal() as session:
        rows = panel_saleable_rows(session)
    if not rows:
        return "هنوز سروری برای فروش باز نشده است. به مدیر مجموعه بگویید سروری را «آماده فروش» کند.", back_button()
    keyboard = [[{"text": f"🛒 {row['name'][:26]}", "callback_data": f"onew|{row['id']}"}] for row in rows[:15]]
    keyboard.append([{"text": "🧾 سفارش‌های من", "callback_data": "section|my_orders"},
                     {"text": "🔙 منو", "callback_data": "section|menu"}])
    lines = ["<b>برای سفارش پنل، سرور خود را انتخاب کنید</b>",
             "پس از انتخاب، نام کسب‌وکار، تعداد کاربر و مصرف روزانه را می‌پرسیم؛ مدیر مجموعه قیمت را تعیین می‌کند."]
    return NL.join(lines), {"inline_keyboard": keyboard}


def order_text(order, telegram_id=None):
    lines = [f"🧾 <b>{html.escape(order['business_name'])}</b>",
             f"سرور: <b>{html.escape(order['panel_name'] or '—')}</b>",
             f"وضعیت: <b>{STATUS_FA.get(order['status'], order['status'])}</b>",
             f"{users(order['user_count'])} · {gib(order['daily_gib'])}"]
    if order["note"]: lines.append(f"توضیح: <i>{html.escape(order['note'][:200])}</i>")
    if order["amount_irr"]:
        lines += ["", f"مبلغ: <b>{money(order['amount_irr'])}</b>", f"سقف اعتبار: <b>{money(order['credit_limit_irr'])}</b>",
                  f"قیمت هر گیگ: <b>{money(order['price_per_gib_irr'])}</b>"]
    if order["status"] in ("quoted", "confirmed", "payment_declared") and order["payment_instructions"]:
        lines += ["", f"راهنمای پرداخت:{NL}{html.escape(order['payment_instructions'][:600])}"]
    if order["rejection_reason"]: lines.append(f"دلیل رد: <i>{html.escape(order['rejection_reason'][:200])}</i>")
    if telegram_id: lines += ["", f"شناسه تلگرام سفارش‌دهنده: <code>{telegram_id}</code>"]
    return NL.join(lines)


def order_buttons(order, owner, reviewer):
    rows = []; status = order["status"]; key = f"o|{order['id']}"
    if owner:
        if status == "quoted": rows.append([{"text": "✅ تأیید قیمت", "callback_data": f"{key}|confirm"}])
        if status == "confirmed": rows.append([{"text": "💳 اعلام پرداخت", "callback_data": f"{key}|pay"}])
        if status in ("pending", "quoted", "confirmed"): rows.append([{"text": "❌ لغو سفارش", "callback_data": f"{key}|cancel"}])
    if reviewer and status == "pending":
        rows.append([{"text": "💵 قیمت‌گذاری", "callback_data": f"{key}|quote"}, {"text": "❌ رد", "callback_data": f"{key}|no"}])
    if reviewer and status == "payment_declared":
        rows.append([{"text": "✅ تأیید و ساخت حساب", "callback_data": f"{key}|ok"}, {"text": "❌ رد", "callback_data": f"{key}|no"}])
    rows.append([{"text": "🔙 برگرد", "callback_data": "section|my_orders" if owner and not reviewer else "section|orders"}])
    return {"inline_keyboard": rows}


def place_order(chat_id, panel_id, business_name, user_count, daily_gib, note):
    """A chat with no membership at all can reach here; that is the whole point of the funnel."""
    with SessionLocal() as session:
        try:
            order = create_panel_order(session, telegram_id=chat_id, display_name=business_name, via="telegram",
                                       panel_id=panel_id, business_name=business_name, user_count=user_count,
                                       daily_gib=daily_gib, note=note)
            session.commit()
        except HTTPException as exc:
            raise PermissionError(str(exc.detail)) from exc
        return order_row(session, order)


def order_detail(chat_id, order_id):
    with SessionLocal() as session:
        order = session.get(PanelOrder, order_id)
        if not order: raise PermissionError("سفارش پیدا نشد.")
        row = order_row(session, order)
        owner = bool(session.scalar(select(Actor.id).where(Actor.id == order.actor_id, Actor.telegram_id == chat_id)))
        ctx = context(chat_id)
        reviewer = bool(ctx and can(ctx["role"], "decide_orders")
                        and session.scalar(select(PanelOwner).where(PanelOwner.panel_id == order.panel_id,
                                                                     PanelOwner.organization_id == ctx["organization_id"])))
        if not owner and not reviewer: raise PermissionError("این سفارش به حساب شما نیست.")
        actor = session.get(Actor, order.actor_id)
        return order_text(row, actor.telegram_id if reviewer and actor else None), order_buttons(row, owner, reviewer)


def order_queue(chat_id, status="pending"):
    if status not in ("pending", "quoted", "confirmed", "payment_declared"): status = "pending"
    ctx = require_context(chat_id)
    if not can(ctx["role"], "decide_orders"): raise PermissionError(denial_fa("decide_orders"))
    with SessionLocal() as session:
        rows = session.scalars(select(PanelOrder).where(PanelOrder.status == status,
                                                        PanelOrder.panel_id.in_(review_panels(session, ctx["organization_id"])))
                               .order_by(PanelOrder.created_at).limit(20)).all()
        lines = [f"<b>سفارش‌های پنل — {STATUS_FA.get(status, status)}</b>"]; keyboard = []
        for order in rows:
            row = order_row(session, order)
            actor = session.get(Actor, order.actor_id)
            linked = f" · <code>{actor.telegram_id}</code>" if actor else ""
            lines.append(f"• <b>{html.escape(row['business_name'])}</b> روی {html.escape(row['panel_name'] or '—')} — "
                         f"{users(row['user_count'])}، {gib(row['daily_gib'])}{linked}")
            extra = [{"text": "💵 قیمت‌گذاری", "callback_data": f"o|{order.id}|quote"}] if status == "pending" else \
                    [{"text": "✅ تأیید پرداخت", "callback_data": f"o|{order.id}|ok"}] if status == "payment_declared" else []
            keyboard.append([{"text": f"📋 {row['business_name'][:16]}", "callback_data": f"o|{order.id}|view"}, *extra])
        if not rows: lines.append("موردی در این وضعیت وجود ندارد.")
    filters = [{"text": "⏳ در انتظار بررسی", "callback_data": "q|pending"}, {"text": "💵 قیمت‌شده", "callback_data": "q|quoted"}]
    keyboard.append([{"text": "💳 در انتظار پرداخت", "callback_data": "q|payment_declared"}])
    keyboard.append([*filters])
    keyboard.append([{"text": "🔙 منو", "callback_data": "section|menu"}])
    return NL.join(lines), {"inline_keyboard": keyboard}


CUSTOMER_ACTIONS = {"confirm": ("confirmed", "قیمت تأیید شد. راهنمای پرداخت را در جزئیات سفارش ببینید."),
                    "pay": ("payment_declared", "اعلام پرداخت ثبت شد. پس از بررسی، حساب شما ساخته می‌شود."),
                    "cancel": ("cancelled", "سفارش لغو شد.")}


def customer_order_action(chat_id, order_id, action):
    """One of the three steps the requester owns; returns the reply and the seller to notify."""
    permission, reply = CUSTOMER_ACTIONS[action]
    with SessionLocal() as session:
        try:
            order = locked_order(session, order_id, permission)
        except HTTPException as exc:
            raise PermissionError(str(exc.detail)) from exc
        actor = requester_actor(session, chat_id)
        if not actor or order.actor_id != actor.id: raise PermissionError("این سفارش به حساب شما نیست.")
        seller_org = session.scalar(select(PanelOwner.organization_id).where(PanelOwner.panel_id == order.panel_id))
        advance_customer_order(session, order, permission)
        session.commit()
        return reply, order_row(session, order), seller_org


def reviewer_order_action(chat_id, order_id, action, reason=""):
    """Approve or reject a declared payment; approval is the only place the funnel creates money."""
    ctx = require_context(chat_id)
    if not can(ctx["role"], "decide_orders"): raise PermissionError(denial_fa("decide_orders"))
    with SessionLocal() as session:
        try:
            order = locked_order(session, order_id, "approved" if action == "ok" else "rejected")
            if action == "ok":
                result = approve_panel_order(session, order, ctx["actor_id"], ctx["organization_id"])
            else:
                reject_panel_order(session, order, ctx["actor_id"], ctx["organization_id"], reason)
                result = {"id": order.id, "status": order.status}
        except HTTPException as exc:
            raise PermissionError(str(exc.detail)) from exc
        session.commit()
        return result


def apply_quote(chat_id, data):
    """Turn the collected answers into a validated quote through the shared API core."""
    payload = OrderQuoteIn(amount_irr=data["amount_irr"], credit_limit_irr=data["credit_limit_irr"],
                           price_per_gib_irr=data["price_per_gib_irr"], payment_instructions=data["instructions"],
                           pg_admin_id=data.get("pg_admin_id"))
    ctx = require_context(chat_id)
    if not can(ctx["role"], "decide_orders"): raise PermissionError(denial_fa("decide_orders"))
    with SessionLocal() as session:
        try:
            order = locked_order(session, data["order_id"], "quoted")
            quote_panel_order(session, order, payload, ctx["actor_id"], ctx["organization_id"])
        except HTTPException as exc:
            raise PermissionError(str(exc.detail)) from exc
        session.commit()
        return order_row(session, order)


def order_owner_telegram_id(order_id):
    with SessionLocal() as session:
        order = session.get(PanelOrder, order_id)
        actor = session.get(Actor, order.actor_id) if order else None
        return actor.telegram_id if actor else None


def staff_chats(organization_id, roles=("system_admin", "reseller_admin")):
    """Telegram ids of the people who can act on an order for one organization."""
    if not organization_id: return []
    with SessionLocal() as session:
        return list(session.execute(select(Actor.telegram_id).join(Membership, Membership.actor_id == Actor.id)
                                    .where(Membership.organization_id == organization_id, Membership.status == "active",
                                           Membership.role.in_(roles))).scalars().all())


def customer_panel(chat_id):
    """What a customer owns: one organization, the server it is bound to and the money behind it."""
    ctx = require_context(chat_id)
    with SessionLocal() as session:
        organization = session.get(Organization, ctx["organization_id"])
        wallet = session.scalar(select(Account).where(Account.owner_key == organization.id, Account.code == "wallet"))
        binding = session.scalar(select(Binding).where(Binding.organization_id == organization.id))
        panel = session.get(Panel, binding.panel_id) if binding else None
        checkpoint = session.get(Checkpoint, binding.id) if binding else None
    usage = int(checkpoint.lifetime_bytes if checkpoint else 0) / 1073741824
    lines = [f"🖥 <b>{html.escape(organization.name)}</b>", "",
             f"سرور: <b>{html.escape(panel.name) if panel else 'هنوز به سروری متصل نشده است'}</b>",
             f"موجودی: <b>{money(wallet.balance_irr if wallet else 0)}</b>",
             f"سقف اعتبار: <b>{money(organization.credit_limit_irr)}</b>",
             f"مصرف: <b>{usage:,.2f} GiB</b>"]
    lines.append("ساخت کاربر و مدیریت مشترکان از پنل انجام می‌شود." if panel
                 else "پس از تأیید پرداخت، مدیر مجموعه سرور را به حساب شما متصل می‌کند.")
    keyboard = [[{"text": "🌐 پنل کاربری", "callback_data": "section|my_webapp"},
                 {"text": "📈 مصرف و تراکنش", "callback_data": "section|usage"}],
                [{"text": "💳 درخواست شارژ", "callback_data": "section|topup"},
                 {"text": "🧾 سفارش‌های من", "callback_data": "section|my_orders"}],
                [{"text": "🔙 منو", "callback_data": "section|menu"}]]
    return NL.join(lines), {"inline_keyboard": keyboard}


# --------------------------------------------------------------------------------------
# Chat state machines
# --------------------------------------------------------------------------------------

async def set_state(redis, chat_id, state):
    await redis.setex(f"bot-state:{chat_id}", STATE_TTL, json.dumps(state, ensure_ascii=False))


async def get_state(redis, chat_id):
    raw = await redis.get(f"bot-state:{chat_id}")
    return json.loads(raw) if raw else None


async def clear_state(redis, chat_id):
    await redis.delete(f"bot-state:{chat_id}")


async def begin_reseller(redis, chat_id):
    await set_state(redis, chat_id, {"flow": "reseller", "step": "name", "data": {}})
    await send(chat_id, "نام نماینده را ارسال کنید. برای لغو /cancel را بفرستید.")


async def begin_order(redis, chat_id, panel_id):
    await set_state(redis, chat_id, {"flow": "order", "step": "business_name", "data": {"panel_id": panel_id}})
    await send(chat_id, "نام کسب‌وکار خود را ارسال کنید. برای لغو /cancel را بفرستید.")


async def begin_quote(redis, chat_id, order_id):
    await set_state(redis, chat_id, {"flow": "quote", "step": "amount", "data": {"order_id": order_id}})
    await send(chat_id, "مبلغ کل این سفارش را به ریال ارسال کنید. برای لغو /cancel را بفرستید.")


def number(value):
    """Accept Persian digits and thousands separators, because that is what people type."""
    digits = "۰۱۲۳۴۵۶۷۸۹"
    return value.translate(str.maketrans(digits, "0123456789")).replace(",", "").strip()


async def abort_flow(redis, chat_id, message):
    await clear_state(redis, chat_id)
    await send(chat_id, html.escape(message), menu_for(await current_role(chat_id)))


async def continue_order(redis, chat_id, value, state):
    data = state["data"]; step = state["step"]
    if step == "business_name":
        if len(value) < 2: await send(chat_id, "نام باید حداقل دو حرف باشد."); return
        data["business_name"] = value[:160]; state["step"] = "user_count"
        await set_state(redis, chat_id, state)
        await send(chat_id, "تعداد کاربر (یوزر) موردنیاز را عددی ارسال کنید؛ مثال: 50")
    elif step == "user_count":
        digits = number(value)
        if not digits.isdigit() or not 0 < int(digits) <= 100000:
            await send(chat_id, "تعداد کاربر معتبر ارسال کنید."); return
        data["user_count"] = int(digits); state["step"] = "daily_gib"
        await set_state(redis, chat_id, state)
        await send(chat_id, "مصرف روزانه هر کاربر را به گیگابایت ارسال کنید؛ مثال: 3")
    elif step == "daily_gib":
        digits = number(value)
        if not digits.isdigit() or not 0 < int(digits) <= 100000:
            await send(chat_id, "مصرف روزانه معتبر ارسال کنید."); return
        data["daily_gib"] = int(digits); state["step"] = "note"
        await set_state(redis, chat_id, state)
        await send(chat_id, "توضیح اختیاری برای مدیر بنویسید یا «بعد» را بفرستید.")
    elif step == "note":
        data["note"] = "" if value in ("بعد", "-", "0") else value[:500]
        try:
            order = await asyncio.to_thread(place_order, chat_id, data["panel_id"], data["business_name"],
                                            data["user_count"], data["daily_gib"], data["note"])
        except PermissionError as exc:
            await abort_flow(redis, chat_id, str(exc)); return
        await clear_state(redis, chat_id)
        await send(chat_id, "سفارش شما ثبت شد و برای بررسی به مدیر مجموعه ارسال شد.{NL}{NL}".format(NL=NL) + order_text(order))
        for chat in await asyncio.to_thread(order_reviewer_chats, order["panel_id"]):
            if chat != chat_id:
                await send(chat, f"🧾 سفارش جدید: <b>{html.escape(order['business_name'])}</b> — "
                                 f"{users(order['user_count'])}، {gib(order['daily_gib'])}",
                           {"inline_keyboard": [[{"text": "💵 قیمت‌گذاری", "callback_data": f"o|{order['id']}|quote"},
                                                 {"text": "📋 جزئیات", "callback_data": f"o|{order['id']}|view"}]]})
        await send(chat_id, "برای پیگیری وضعیت، «🧾 سفارش‌های من» را بزنید.", menu_for(CUSTOMER))


QUOTE_STEPS = {"amount": ("amount_irr", "credit", "مبلغ کل این سفارش را به ریال ارسال کنید."),
               "credit": ("credit_limit_irr", "price", "سقف اعتبار (حد مجاز بدهی) را به ریال ارسال کنید؛ برای بدون اعتبار 0 بفرستید."),
               "price": ("price_per_gib_irr", "instructions", "قیمت هر گیگابایت را به ریال ارسال کنید."),
               "instructions": (None, "pg_admin_id", "شماره کارت یا راهنمای پرداخت را ارسال کنید؛ مشتری همین متن را می‌بیند."),
               "pg_admin_id": (None, None, None)}


async def continue_quote(redis, chat_id, value, state):
    data = state["data"]; step = state["step"]
    field, nxt, _prompt = QUOTE_STEPS[step]
    if field:
        digits = number(value)
        if not digits.isdigit() or (int(digits) <= 0 and field != "credit_limit_irr"):
            await send(chat_id, "عدد معتبر به ریال ارسال کنید."); return
        data[field] = int(digits)
    elif step == "instructions":
        if len(value) < 3: await send(chat_id, "راهنمای پرداخت را کامل‌تر بنویسید."); return
        data["instructions"] = value[:2000]
    else:
        skip = value in ("بعد", "-", "0")
        digits = number(value)
        if not skip and not digits.isdigit(): await send(chat_id, "شناسه Admin باید عددی باشد."); return
        data["pg_admin_id"] = None if skip else int(digits)
        try:
            quoted = await asyncio.to_thread(apply_quote, chat_id, data)
        except PermissionError as exc:
            await abort_flow(redis, chat_id, str(exc)); return
        await clear_state(redis, chat_id)
        await send(chat_id, "قیمت سفارش ثبت و برای مشتری ارسال شد.", menu_for(await current_role(chat_id)))
        customer = await asyncio.to_thread(order_owner_telegram_id, quoted["id"])
        if customer:
            await send(customer, "💵 سفارش شما قیمت‌گذاری شد.{NL}{NL}".format(NL=NL) + order_text(quoted),
                       {"inline_keyboard": [[{"text": "✅ تأیید قیمت", "callback_data": f"o|{quoted['id']}|confirm"},
                                             {"text": "📋 جزئیات", "callback_data": f"o|{quoted['id']}|view"}]]})
        return
    state["step"] = nxt
    await set_state(redis, chat_id, state)
    await send(chat_id, {"credit": "سقف اعتبار (حد مجاز بدهی) را به ریال ارسال کنید؛ برای بدون اعتبار 0 بفرستید.",
                         "price": "قیمت هر گیگابایت را به ریال ارسال کنید.",
                         "instructions": "شماره کارت یا راهنمای پرداخت را ارسال کنید؛ مشتری همین متن را می‌بیند.",
                         "pg_admin_id": "شناسه عددی Admin این مشتری در پنل مادر را بفرستید؛ اگر نساخته‌اید «بعد» را بزنید."}[nxt])


def order_reviewer_chats(panel_id):
    """Who has to review an order placed against one server."""
    with SessionLocal() as session:
        organization_id = session.scalar(select(PanelOwner.organization_id).where(PanelOwner.panel_id == panel_id))
    return staff_chats(organization_id)


async def continue_state(redis, chat_id, value, state):
    if value == "/cancel":
        await clear_state(redis, chat_id)
        await send(chat_id, "عملیات لغو شد.", menu_for(await current_role(chat_id))); return
    if state["flow"] == "order": await continue_order(redis, chat_id, value, state); return
    if state["flow"] == "quote": await continue_quote(redis, chat_id, value, state); return
    if state["flow"] == "credit":
        amount = number(value)
        if not amount.isdigit() or int(amount) <= 0: await send(chat_id, "مبلغ معتبر به ریال ارسال کنید."); return
        ctx = await asyncio.to_thread(require_context, chat_id)
        with SessionLocal() as session:
            session.add(FundingRequest(organization_id=ctx["organization_id"], actor_id=ctx["actor_id"], amount_irr=int(amount)))
            session.commit()
        await clear_state(redis, chat_id)
        await send(chat_id, "درخواست افزایش اعتبار ثبت شد و برای مدیر مجموعه ارسال شد.", menu_for(await current_role(chat_id)))
        for chat in await asyncio.to_thread(staff_chats, ctx["organization_id"],
                                            ("system_admin", "reseller_admin", "finance")):
            if chat != chat_id:
                await send(chat, f"💳 درخواست شارژ برای <b>{html.escape(ctx['organization_name'])}</b> — {money(int(amount))}")
        return
    data = state["data"]; step = state["step"]
    if step == "name":
        if len(value) < 2: await send(chat_id, "نام باید حداقل دو حرف باشد."); return
        data["name"] = value; state["step"] = "slug"; await set_state(redis, chat_id, state)
        await send(chat_id, "شناسه انگلیسی نماینده را ارسال کنید؛ مثال: reseller-north")
    elif step == "slug":
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,78}[a-z0-9]", value):
            await send(chat_id, "شناسه فقط باید شامل حروف کوچک انگلیسی، عدد و خط تیره باشد."); return
        data["slug"] = value; state["step"] = "telegram_id"; await set_state(redis, chat_id, state)
        await send(chat_id, "شناسه عددی تلگرام نماینده را ارسال کنید. نماینده می‌تواند با دستور /id آن را ببیند.")
    elif step == "telegram_id":
        if not value.isdigit(): await send(chat_id, "شناسه تلگرام باید عددی باشد."); return
        data["telegram_id"] = value; state["step"] = "price"; await set_state(redis, chat_id, state)
        await send(chat_id, "قیمت هر گیگابایت را به ریال ارسال کنید.")
    elif step == "price":
        if not number(value).isdigit() or int(number(value)) <= 0: await send(chat_id, "قیمت معتبر ارسال کنید."); return
        data["price"] = number(value); state["step"] = "credit"; await set_state(redis, chat_id, state)
        await send(chat_id, "سقف اعتبار اولیه را به ریال ارسال کنید؛ برای بدون اعتبار عدد 0 را بفرستید.")
    elif step == "credit":
        if not number(value).isdigit(): await send(chat_id, "اعتبار معتبر ارسال کنید."); return
        data["credit"] = number(value)
        try:
            name = await asyncio.to_thread(create_reseller, chat_id, data)
            await clear_state(redis, chat_id)
            await send(chat_id, f"نماینده <b>{html.escape(name)}</b> ساخته و حساب تلگرام متصل شد.", menu_for(await current_role(chat_id)))
        except Exception as exc:
            await clear_state(redis, chat_id)
            await send(chat_id, f"ساخت نماینده انجام نشد: {html.escape(str(exc))}", menu_for(await current_role(chat_id)))


async def current_role(chat_id):
    """Staff roles come from the active membership; everyone else is a prospective customer."""
    ctx = await asyncio.to_thread(context, chat_id)
    return ctx["role"] if ctx else CUSTOMER


async def open_section(chat_id, section, redis):
    """Render one menu section; the role decides both the entry and the refusal."""
    role = await current_role(chat_id)
    permission = SECTION_PERMISSION.get(section)
    if not permission or section not in sections_for(role):
        await send(chat_id, denial_fa(permission or "view_dashboard"), menu_for(role)); return
    menu = menu_for(role)
    if section == "dashboard":
        await send(chat_id, await asyncio.to_thread(dashboard, chat_id, can(role, "manage_resellers")), menu)
    elif section == "servers":
        message, markup = await asyncio.to_thread(server_list, chat_id); await send(chat_id, message, markup)
    elif section == "orders":
        message, markup = await asyncio.to_thread(order_queue, chat_id); await send(chat_id, message, markup)
    elif section == "resellers":
        message, markup = await asyncio.to_thread(reseller_list, chat_id); await send(chat_id, message, markup)
    elif section == "finance":
        message, markup = await asyncio.to_thread(finance_menu, chat_id); await send(chat_id, message, markup)
    elif section == "new_order":
        message, markup = await asyncio.to_thread(order_panels, chat_id); await send(chat_id, message,markup)
    elif section == "my_orders":
        rows = await asyncio.to_thread(customer_order_rows, chat_id)
        if not rows:
            await send(chat_id, "شما هنوز سفارشی ثبت نکرده‌اید. با «➕ سفارش پنل» اولین درخواست خود را بفرستید.", menu); return
        lines = ["<b>سفارش‌های شما</b>"]; keyboard = []
        for row in rows:
            lines.append(f"• {html.escape(row['business_name'])} — {STATUS_FA.get(row['status'], row['status'])}")
            keyboard.append([{"text": f"📋 {row['business_name'][:20]}", "callback_data": f"o|{row['id']}|view"}])
        keyboard.append([{"text": "➕ سفارش پنل", "callback_data": "section|new_order"},
                         {"text": "🔙 منو", "callback_data": "section|menu"}])
        await send(chat_id, NL.join(lines), {"inline_keyboard": keyboard})
    elif section in ("my_panel", "usage", "topup", "my_webapp"):
        ctx = await asyncio.to_thread(context, chat_id)
        if not ctx:
            await send(chat_id, "شما هنوز حسابی ندارید. با «➕ سفارش پنل» درخواست بدهید تا پس از تأیید پرداخت، حساب شما ساخته شود.", menu); return
        if section == "my_panel":
            message, markup = await asyncio.to_thread(customer_panel, chat_id); await send(chat_id, message, markup)
        elif section == "usage":
            await send(chat_id, await asyncio.to_thread(transactions, chat_id), menu)
        elif section == "topup":
            await set_state(redis, chat_id, {"flow": "credit", "step": "amount", "data": {}})
            await send(chat_id, "مبلغ درخواستی را به ریال ارسال کنید. برای لغو /cancel را بفرستید.")
        else:
            await send(chat_id, "ساخت کاربر، مدیریت مشترکان و گزارش مصرف در پنل شما:", panel_button("باز کردن پنل من"))
    elif section == "new_reseller":
        await begin_reseller(redis, chat_id)
    elif section == "funding":
        message, markup = await asyncio.to_thread(funding_requests, chat_id); await send(chat_id, message, markup or menu)
    elif section == "transactions":
        await send(chat_id, await asyncio.to_thread(transactions, chat_id), back_button("🔙 مرکز مالی", "finance"))
    elif section == "credit":
        await set_state(redis, chat_id, {"flow": "credit", "step": "amount", "data": {}})
        await send(chat_id, "مبلغ درخواستی را به ریال ارسال کنید. برای لغو /cancel را بفرستید.")
    elif section == "approvals":
        message, markup = await asyncio.to_thread(approval_requests, chat_id); await send(chat_id, message, markup or menu)
    elif section == "system":
        access = "، ".join(LABELS[name] for name in sections_for(role)) or "فقط مشاهده"
        await send(chat_id, f"نقش شما: <b>{html.escape(role_fa(role))}</b>{NL}شناسهٔ تلگرام: <code>{chat_id}</code>"
                           f"{NL}دسترسی‌ها: {html.escape(access)}", panel_button("وب‌اپ مدیریت"))
    elif section == "support":
        await send(chat_id, f"تیکت پشتیبانی برای مدیر مجموعه ارسال شد. شناسهٔ شما: <code>{chat_id}</code>"
                            f"{NL}برای پیگیری، همین شناسه را نگه دارید.", menu)
    elif section == "webapp":
        await send(chat_id, "ورود به پنل:", panel_button("باز کردن پنل"))
    else:
        await send(chat_id, "گزینه معتبر را از منوی خود انتخاب کنید.", menu)


async def notify_chats(chats, text, markup=None, except_chat=None):
    for chat in chats:
        if chat != except_chat: await send(chat, text, markup)


async def handle_message(redis, chat_id, value):
    value = (value or "").strip()
    if value == "/id":
        await send(chat_id, f"شناسه عددی تلگرام شما: <code>{chat_id}</code>"); return
    state = await get_state(redis, chat_id)
    if state: await continue_state(redis, chat_id, value, state); return
    role = await current_role(chat_id)
    if value == "/start":
        if await asyncio.to_thread(context, chat_id):
            await send(chat_id, "خوش آمدید. منوی اختصاصی نقش شما فعال شد.", menu_for(role))
        else:
            await send(chat_id, "به سرویس پنل پاسارگارد خوش آمدید."
                                "{NL}{NL}برای داشتن پنل فروش روی «➕ سفارش پنل» بزنید: سرور را انتخاب می‌کنید، تعداد کاربر "
                                "و مصرف روزانه را می‌گویید، قیمت را از مدیر می‌گیرید و پس از پرداخت حساب شما ساخته می‌شود."
                                "{NL}{NL}اگر کارکنان یا نمایندهٔ مجموعه هستید، مدیر مجموعه حساب شما را متصل می‌کند.".format(NL=NL),
                         menu_for(role))
        await open_section(chat_id, "new_order" if role == CUSTOMER else "dashboard", redis)
        return
    if value in ALIASES:
        target = ALIASES[value]
        if target == "menu":
            await send(chat_id, "بخش موردنظر را انتخاب کنید:", section_menu(role)); return
        await open_section(chat_id, target, redis); return
    section = label_to_section(value)
    if not section or section not in sections_for(role):
        await send(chat_id, "گزینه معتبر را از منوی خود انتخاب کنید.", menu_for(role)); return
    await open_section(chat_id, section, redis)


async def handle_callback(redis, chat_id, callback_id, data):
    try:
        parts = data.split("|")
        if parts[0] == "section" and len(parts) == 2:
            await answer_callback(callback_id, "در حال بارگذاری…")
            if parts[1] == "menu":
                await send(chat_id, "بخش موردنظر را انتخاب کنید:", section_menu(await current_role(chat_id)))
            else:
                await open_section(chat_id, parts[1], redis)
        elif parts[0] == "nodes" and len(parts) == 2:
            message, markup = await asyncio.to_thread(node_list, chat_id, parts[1]); await answer_callback(callback_id)
            await send(chat_id, message, markup)
        elif parts[0] == "node" and len(parts) == 4:
            message = await asyncio.to_thread(node_action, chat_id, parts[1], parts[2], parts[3])
            await answer_callback(callback_id, message if parts[3] != "status" else "وضعیت ارسال شد")
            if parts[3] == "status": await send(chat_id, f"<b>وضعیت نود</b>{NL}<code>{message}</code>")
        elif parts[0] in ("fundok", "fundno") and len(parts) == 2:
            message, customer = await asyncio.to_thread(decide_funding, chat_id, parts[1], parts[0] == "fundok")
            await answer_callback(callback_id, message)
            await send(chat_id, message, menu_for(await current_role(chat_id)))
            if customer: await send(customer, f"درخواست شارژ شما {'تأیید و کیف پول شارژ شد' if parts[0]=='fundok' else 'رد شد'}.")
        elif parts[0] == "appr" and len(parts) == 3:
            message = await asyncio.to_thread(decide_approval, chat_id, parts[1], parts[2] == "ok")
            await answer_callback(callback_id, message); await send(chat_id, message, menu_for(await current_role(chat_id)))
        elif parts[0] == "sale" and len(parts) == 3:
            message = await asyncio.to_thread(set_saleable, chat_id, parts[1], parts[2] == "1")
            await answer_callback(callback_id, message)
            text, markup = await asyncio.to_thread(server_list, chat_id); await send(chat_id, text, markup)
        elif parts[0] == "onew" and len(parts) == 2:
            await answer_callback(callback_id, "در حال ثبت سفارش…"); await begin_order(redis, chat_id, parts[1])
        elif parts[0] == "q" and len(parts) == 2:
            message, markup = await asyncio.to_thread(order_queue, chat_id, parts[1])
            await answer_callback(callback_id); await send(chat_id, message, markup)
        elif parts[0] == "o" and len(parts) == 3:
            await order_callback(redis, chat_id, callback_id, parts[1], parts[2])
        else:
            await answer_callback(callback_id, "دستور نامعتبر")
    except PermissionError as exc:
        # These messages are written for the user; never forward an unexpected exception text.
        print(f"callback {data}: {exc}", flush=True); await answer_callback(callback_id, str(exc)[:190])
    except Exception as exc:
        print(f"callback {data}: {exc}", flush=True); await answer_callback(callback_id, "عملیات انجام نشد")


async def order_callback(redis, chat_id, callback_id, order_id, action):
    if action == "view":
        text, markup = await asyncio.to_thread(order_detail, chat_id, order_id)
        await answer_callback(callback_id); await send(chat_id, text, markup); return
    if action == "quote":
        await answer_callback(callback_id, "در حال قیمت‌گذاری…"); await begin_quote(redis, chat_id, order_id); return
    if action in CUSTOMER_ACTIONS:
        message, row, seller_org = await asyncio.to_thread(customer_order_action, chat_id, order_id, action)
        await answer_callback(callback_id, message); await send(chat_id, message)
        text, markup = await asyncio.to_thread(order_detail, chat_id, order_id)
        await send(chat_id, text, markup)
        if action == "pay" and seller_org:
            await notify_chats(await asyncio.to_thread(staff_chats, seller_org),
                               f"💳 پرداخت سفارش <b>{html.escape(row['business_name'])}</b> اعلام شد. "
                               "پس از بررسی وجه، «تأیید و ساخت حساب» را بزنید.",
                               {"inline_keyboard": [[{"text": "✅ تأیید و ساخت حساب", "callback_data": f"o|{order_id}|ok"},
                                                     {"text": "📋 جزئیات", "callback_data": f"o|{order_id}|view"}]]})
        return
    if action in ("ok", "no"):
        result = await asyncio.to_thread(reviewer_order_action, chat_id, order_id, action,
                                         "" if action == "ok" else "رد توسط مدیر مجموعه")
        await answer_callback(callback_id, "تصمیم ثبت شد")
        await send(chat_id, "✅ حساب مشتری ساخته و کیف پول شارژ شد." if action == "ok" else "❌ سفارش رد شد.",
                   menu_for(await current_role(chat_id)))
        customer = await asyncio.to_thread(order_owner_telegram_id, order_id)
        if not customer: return
        if action == "ok":
            await send(customer, "🎉 سفارش شما تأیید شد و حساب شما ساخته شد."
                                 f"{NL}موجودی اولیه: <b>{money(result.get('balance_irr', 0))}</b>"
                                 f"{NL}سرور: <b>{html.escape(str(result.get('panel_name') or ''))}</b>"
                                 f"{NL}با «🌐 پنل کاربری» وارد پنل شوید و کاربر بسازید.", panel_button("باز کردن پنل من"))
        else:
            await send(customer, "❌ متأسفانه سفارش شما رد شد. برای دیدن دلیل به بخش 🛟 پشتیبانی پیام بدهید.", menu_for(CUSTOMER))


async def main():
    redis = Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)
    try: await redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except Exception: pass
    consumer = os.getenv("HOSTNAME", "telegram-1")
    while True:
        rows = await redis.xreadgroup(GROUP, consumer, {STREAM: ">"}, count=50, block=5000)
        for _, items in rows:
            for message_id, raw in items:
                try:
                    update = json.loads(raw["update"])
                    if update.get("callback_query"):
                        callback = update["callback_query"]; chat_id = callback["message"]["chat"]["id"]
                        await handle_callback(redis, chat_id, callback["id"], callback.get("data", ""))
                    else:
                        message = update.get("message") or {}; chat_id = (message.get("chat") or {}).get("id")
                        if chat_id: await handle_message(redis, chat_id, message.get("text", ""))
                    await redis.xack(STREAM, GROUP, message_id)
                except Exception as exc: print(f"telegram message {message_id}: {exc}", flush=True)


if __name__ == "__main__": asyncio.run(main())
