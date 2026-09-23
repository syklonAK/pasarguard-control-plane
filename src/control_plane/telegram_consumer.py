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
    Membership, Organization, Panel, PanelOwner, SessionLocal, Transaction, account, audit,
    effective_role, now, transfer,
)
from .rbac import can, denial_fa, role_fa
from .web_api import _unwrap, panel_operation

STREAM = "telegram_updates"
GROUP = "telegram-workers"
STATE_TTL = 900
NL = chr(10)

# One table drives both keyboards: a section is offered only when the resolved role holds the
# permission, so the bot never shows an action the API would refuse.
SECTIONS=(
    ("dashboard","📊 داشبورد","view_dashboard"),
    ("servers","🖥 سرورها","manage_servers"),
    ("resellers","👥 نمایندگان","manage_resellers"),
    ("new_reseller","➕ نماینده جدید","manage_resellers"),
    ("funding","💰 درخواست‌های مالی","decide_funding"),
    ("transactions","📜 تراکنش‌ها","view_finance"),
    ("credit","➕ درخواست اعتبار","request_funding"),
    ("approvals","✅ تأیید اصلاحات","decide_adjustment"),
    ("system","⚙️ مدیریت سیستم","view_audit"),
    ("support","🛟 پشتیبانی","create_support_ticket"),
    ("webapp","🌐 وب‌اپ","view_dashboard"),
)
LABELS={name:label for name,label,_ in SECTIONS}
ALIASES={"/dashboard":"dashboard","/servers":"servers","/support":"support","/menu":"menu","/resellers":"resellers","/finance":"transactions"}

def sections_for(role):
    return [name for name,_,permission in SECTIONS if can(role,permission)]

def menu_for(role):
    labels=[LABELS[name] for name in sections_for(role)]
    rows=[labels[i:i+2] for i in range(0,len(labels),2)]
    return {"keyboard":[[{"text":label} for label in row] for row in rows],"resize_keyboard":True,"is_persistent":True}

def section_menu(role):
    names=sections_for(role)
    rows=[[{"text":LABELS[name],"callback_data":f"section|{name}"} for name in chunk] for chunk in (names[i:i+2] for i in range(0,len(names),2))]
    return {"inline_keyboard":rows}

def label_to_section(label):
    for name,item_label,_permission in SECTIONS:
        if item_label==label:return name
    return None

def root_id():
    value=os.getenv("ROOT_TELEGRAM_ID","")
    return int(value) if value.isdigit() else None

def panel_button(label="ورود به پنل"):
    url=os.getenv("WEBAPP_URL","")
    return {"inline_keyboard":[[{"text":label,"web_app":{"url":url}}]]} if url else None

async def telegram(method,payload):
    token=os.environ["TELEGRAM_BOT_TOKEN"]
    url="https:"+"//"+"api.telegram.org"+"/bot"+token+"/"+method
    async with httpx.AsyncClient(timeout=15) as client:
        response=await client.post(url,json=payload);response.raise_for_status();return response.json()

async def send(chat_id,message,reply_markup=None):
    payload={"chat_id":chat_id,"text":message,"parse_mode":"HTML"}
    if reply_markup:payload["reply_markup"]=reply_markup
    await telegram("sendMessage",payload)

async def answer_callback(callback_id,message="انجام شد"):
    await telegram("answerCallbackQuery",{"callback_query_id":callback_id,"text":message})

def context(chat_id):
    with SessionLocal() as session:
        actor=session.scalar(select(Actor).where(Actor.telegram_id==chat_id,Actor.status=="active"))
        if not actor:return None
        membership=session.scalar(select(Membership).where(Membership.actor_id==actor.id,Membership.status=="active"))
        if not membership:return None
        organization=session.get(Organization,membership.organization_id)
        return {"actor_id":actor.id,"organization_id":organization.id,"organization_name":organization.name,
                "role":effective_role(chat_id,membership.role)}

def require_context(chat_id):
    value=context(chat_id)
    if not value:raise PermissionError("Telegram account is not linked")
    return value

def money(value):return f"{int(value or 0):,} ریال"

def dashboard(chat_id,admin=False):
    ctx=require_context(chat_id)
    with SessionLocal() as session:
        organization=session.get(Organization,ctx["organization_id"])
        wallet=session.scalar(select(Account).where(Account.owner_key==organization.id,Account.code=="wallet"))
        binding=session.scalar(select(Binding).where(Binding.organization_id==organization.id))
        checkpoint=session.get(Checkpoint,binding.id) if binding else None
        direct_children=int(session.scalar(select(func.count()).select_from(Organization).where(Organization.parent_id==organization.id)) or 0)
        if admin:
            organizations=int(session.scalar(select(func.count()).select_from(Organization)) or 0)
            panels=int(session.scalar(select(func.count()).select_from(Panel).where(Panel.status=="active")) or 0)
            pending=int(session.scalar(select(func.count()).select_from(FundingRequest).where(FundingRequest.status=="pending")) or 0)
        else:organizations=panels=pending=0
    wallet_balance=int(wallet.balance_irr if wallet else 0);credit=int(organization.credit_limit_irr);usage=int(checkpoint.lifetime_bytes if checkpoint else 0)/1073741824
    title="داشبورد مدیریت" if admin else html.escape(organization.name)
    lines=[f"<b>{title}</b>","",f"موجودی: <b>{money(wallet_balance)}</b>",f"اعتبار: <b>{money(credit)}</b>",f"قابل استفاده: <b>{money(wallet_balance+credit)}</b>",f"مصرف: <b>{usage:,.2f} GiB</b>",f"زیرمجموعه مستقیم: <b>{direct_children}</b>"]
    if admin:lines += ["",f"کل سازمان‌ها: <b>{organizations}</b>",f"سرورهای فعال: <b>{panels}</b>",f"درخواست مالی باز: <b>{pending}</b>"]
    return "\n".join(lines)

def server_list(chat_id):
    ctx=require_context(chat_id)
    with SessionLocal() as session:
        rows=session.execute(text("SELECT p.id,p.name,p.base_url,p.status FROM panels p JOIN panel_owners po ON po.panel_id=p.id WHERE po.organization_id=:org ORDER BY p.name LIMIT 20"),{"org":ctx["organization_id"]}).mappings().all()
    if not rows:return "هنوز سروری ثبت نشده است.",panel_button("ثبت اولین سرور")
    lines=["<b>سرورها</b>"];keyboard=[]
    for row in rows:
        icon="🟢" if row["status"]=="active" else "🔴"
        lines.append(f"{icon} <b>{html.escape(row['name'])}</b>\n<code>{html.escape(row['base_url'])}</code>")
        keyboard.append([{"text":f"نودهای {row['name']}","callback_data":f"nodes|{row['id']}"}])
    keyboard.append([{"text":"➕ ثبت سرور جدید","web_app":{"url":os.getenv("WEBAPP_URL","")}}])
    return "\n\n".join(lines),{"inline_keyboard":keyboard}

def bot_panel(chat_id,panel_id):
    """Resolve a panel the caller owns and hand back the open session that loaded it."""
    ctx=require_context(chat_id)
    session=SessionLocal()
    link=session.scalar(select(PanelOwner).where(PanelOwner.panel_id==panel_id,PanelOwner.organization_id==ctx["organization_id"]))
    panel=session.get(Panel,panel_id) if link else None
    if not panel or panel.status!="active":
        session.close()
        raise PermissionError("سرور پیدا نشد یا غیرفعال است.")
    return session,ctx,panel

def run_panel_operation(session,ctx,panel,action,run,metadata=None):
    """Share one audited code path with the WebApp and turn its errors into Persian replies."""
    try:
        return panel_operation(session,panel,ctx["organization_id"],action,run,ctx["actor_id"],metadata)
    except HTTPException as exc:
        raise PermissionError(str(exc.detail)) from exc

def node_list(chat_id,panel_id):
    session,ctx,panel=bot_panel(chat_id,panel_id)
    try:
        nodes=_unwrap(run_panel_operation(session,ctx,panel,"panel.nodes.read",lambda c:c.nodes()),"nodes")
    finally:
        session.close()
    lines=[f"<b>نودهای {html.escape(panel.name)}</b>"];keyboard=[]
    for node in nodes[:20]:
        node_id=node.get("id")
        if node_id is None:continue
        name=str(node.get("name") or f"Node {node_id}")
        enabled=node.get("enable",node.get("status"))
        state="🟢" if enabled in (True,"online","active") else "⚪"
        lines.append(f"{state} • {html.escape(name)} — <code>{html.escape(str(node.get('message') or node.get('status') or ''))}</code>")
        keyboard.append([{"text":f"🔄 {name[:22]}","callback_data":f"node|{panel_id}|{node_id}|reconnect"},
                         {"text":("⏸" if enabled in (True,"online","active") else "▶️")+" "+name[:20],
                          "callback_data":f"node|{panel_id}|{node_id}|{'disable' if enabled in (True,'online','active') else 'enable'}"}])
        keyboard.append([{"text":f"♻️ بازنشانی {name[:22]}","callback_data":f"node|{panel_id}|{node_id}|reset"},
                         {"text":f"📊 وضعیت {name[:22]}","callback_data":f"node|{panel_id}|{node_id}|status"}])
    if not keyboard:lines.append("نودی دریافت نشد.")
    return "\n".join(lines),{"inline_keyboard":keyboard} if keyboard else None

NODE_OPERATIONS={
    "enable":("node.enable",lambda i,c:c.set_node_enabled(i,True),"نود روشن شد."),
    "disable":("node.disable",lambda i,c:c.set_node_enabled(i,False),"نود خاموش شد."),
    "reconnect":("node.reconnect",lambda i,c:c.reconnect_node(i),"دستور اتصال مجدد ارسال شد."),
    "reset":("node.reset",lambda i,c:c.reset_node(i),"دستور بازنشانی نود ارسال شد."),
}

def node_action(chat_id,panel_id,node_id,action):
    """Runs one node operation and returns the Persian confirmation, or the status text."""
    if action=="status":
        session,ctx,panel=bot_panel(chat_id,panel_id)
        try:
            status=run_panel_operation(session,ctx,panel,"node.status.read",lambda c:c.node_status(int(node_id)),{"node_id":int(node_id)})
        finally:
            session.close()
        return html.escape(json.dumps(status,ensure_ascii=False)[:900])
    if action not in NODE_OPERATIONS:raise PermissionError("عملیات نامعتبر است.")
    audit_action,call,reply=NODE_OPERATIONS[action]
    session,ctx,panel=bot_panel(chat_id,panel_id)
    try:
        run_panel_operation(session,ctx,panel,audit_action,lambda c:call(int(node_id),c),{"node_id":int(node_id)})
    finally:
        session.close()
    return reply

def reseller_list(chat_id,admin=False):
    ctx=require_context(chat_id)
    with SessionLocal() as session:
        rows=session.scalars(select(Organization).where(Organization.parent_id==ctx["organization_id"]).order_by(Organization.created_at.desc()).limit(30)).all();output=[]
        for organization in rows:
            wallet=session.scalar(select(Account).where(Account.owner_key==organization.id,Account.code=="wallet"))
            actor=session.scalar(select(Actor).join(Membership,Membership.actor_id==Actor.id).where(Membership.organization_id==organization.id))
            output.append((organization,int(wallet.balance_irr if wallet else 0),actor.telegram_id if actor else None))
    if not output:return "هنوز زیرمجموعه‌ای ایجاد نشده است."
    lines=["<b>نمایندگان</b>" if admin else "<b>زیرمجموعه‌ها</b>"]
    for organization, credit_used, telegram_id in output:
        telegram_text=f" — <code>{telegram_id}</code>" if telegram_id else " — بدون اتصال تلگرام"
        lines.append(f"• <b>{html.escape(organization.name)}</b> — {money(credit_used)}{telegram_text}")
    return "\n".join(lines)

def transactions(chat_id):
    ctx=require_context(chat_id)
    with SessionLocal() as session:
        wallet=session.scalar(select(Account).where(Account.owner_key==ctx["organization_id"],Account.code=="wallet"))
        if not wallet:return "هنوز تراکنشی ثبت نشده است."
        rows=session.execute(select(Transaction,Entry).join(Entry,Entry.transaction_id==Transaction.id).where(Entry.account_id==wallet.id).order_by(Transaction.created_at.desc()).limit(15)).all()
    if not rows:return "هنوز تراکنشی ثبت نشده است."
    lines=["<b>آخرین تراکنش‌ها</b>"]
    for transaction,entry in rows:
        sign="+" if entry.side=="credit" else "-";lines.append(f"{sign}{money(entry.amount_irr)} — {html.escape(transaction.kind)}")
    return "\n".join(lines)

SECTION_PERMISSION={name:permission for name,_,permission in SECTIONS}

def funding_requests(chat_id):
    ctx=require_context(chat_id)
    if not can(ctx["role"],"decide_funding"):raise PermissionError(denial_fa("decide_funding"))
    with SessionLocal() as session:
        rows=session.execute(select(FundingRequest,Organization).join(Organization,Organization.id==FundingRequest.organization_id)
                             .where(FundingRequest.status=="pending",Organization.parent_id==ctx["organization_id"])
                             .order_by(FundingRequest.created_at).limit(20)).all()
    if not rows:return "درخواست مالی بازی وجود ندارد.",None
    lines=["<b>درخواست‌های افزایش اعتبار زیرمجموعه‌های شما</b>"];keyboard=[]
    for request,organization in rows:
        lines.append(f"• {html.escape(organization.name)} — <b>{money(request.amount_irr)}</b>")
        keyboard.append([{"text":f"✅ تایید {organization.name[:18]}","callback_data":f"fundok|{request.id}"},
                         {"text":"❌ رد","callback_data":f"fundno|{request.id}"}])
    keyboard.append([{"text":"🔙 منو","callback_data":"section|dashboard"}])
    return "\n".join(lines),{"inline_keyboard":keyboard}

def decide_funding(chat_id,request_id,approve):
    """The direct parent releases the credit; no other branch can spend this money."""
    ctx=require_context(chat_id)
    if not can(ctx["role"],"decide_funding"):raise PermissionError(denial_fa("decide_funding"))
    with SessionLocal() as session:
        request=session.scalar(select(FundingRequest).where(FundingRequest.id==request_id).with_for_update())
        if not request or request.status!="pending":return "این درخواست قبلاً بررسی شده است."
        child=session.get(Organization,request.organization_id)
        if not child or child.parent_id!=ctx["organization_id"]:raise PermissionError("این درخواست به شاخهٔ دیگری تعلق دارد.")
        if approve:
            transfer(session,"SYSTEM",request.organization_id,request.amount_irr,f"funding-request:{request.id}","fund",request.id,False,ctx["actor_id"])
            request.status="approved"
        else:request.status="rejected"
        request.decided_by=ctx["actor_id"];request.decided_at=now()
        audit(session,f"funding_request.{'approve' if approve else 'reject'}","funding_request",request.id,
              actor_id=ctx["actor_id"],organization_id=request.organization_id,metadata={"amount_irr":request.amount_irr})
        session.commit()
    return "درخواست تایید و کیف پول شارژ شد." if approve else "درخواست رد شد."

def approval_requests(chat_id):
    """Wallet corrections wait here for the only role allowed to approve them."""
    ctx=require_context(chat_id)
    if not can(ctx["role"],"decide_adjustment"):raise PermissionError(denial_fa("decide_adjustment"))
    with SessionLocal() as session:
        rows=session.scalars(select(ApprovalRequest).where(ApprovalRequest.status=="pending").order_by(ApprovalRequest.created_at).limit(20)).all()
        names={organization.id:organization.name for organization in session.scalars(select(Organization)).all()}
    if not rows:return "درخواست تأیید بازی وجود ندارد.",None
    lines=["<b>اصلاح حساب‌های در انتظار تأیید</b>"];keyboard=[]
    for approval in rows:
        amount=int(approval.payload["amount_irr"])
        lines.append(f"• {html.escape(names.get(approval.target_id,approval.target_id))} — <b>{money(amount)}</b>{NL}<i>{html.escape(str(approval.payload['note'])[:160])}</i>")
        keyboard.append([{"text":"✅ تأیید","callback_data":f"appr|{approval.id}|ok"},{"text":"❌ رد","callback_data":f"appr|{approval.id}|no"}])
    keyboard.append([{"text":"🔙 منو","callback_data":"section|dashboard"}])
    return "\n".join(lines),{"inline_keyboard":keyboard}

def decide_approval(chat_id,approval_id,approve):
    ctx=require_context(chat_id)
    if not can(ctx["role"],"decide_adjustment"):raise PermissionError(denial_fa("decide_adjustment"))
    from .web_api import APPROVAL_APPLICATORS, locked_pending_approval
    from .outbox import enqueue
    with SessionLocal() as session:
        approval=locked_pending_approval(session,approval_id,now())
        if approve:
            applicator=APPROVAL_APPLICATORS.get(approval.action)
            if not applicator:raise PermissionError("این نوع درخواست قابل اجرا نیست.")
            applicator(session,approval,ctx["actor_id"])
            approval.status="executed"
        else:approval.status="rejected"
        approval.approver_id=ctx["actor_id"];approval.decided_at=now()
        audit(session,f"approval.{'approve' if approve else 'reject'}","approval_request",approval.id,actor_id=ctx["actor_id"],
              organization_id=ctx["organization_id"],metadata={"action":approval.action,"target_id":approval.target_id})
        enqueue(session,"approval.granted",approval.id,{"action":approval.action,"status":approval.status})
        session.commit()
    return "اصلاح حساب اعمال شد." if approve else "درخواست رد شد."

def create_reseller(chat_id,data):
    ctx=require_context(chat_id)
    if not can(ctx["role"],"manage_resellers"):raise PermissionError(denial_fa("manage_resellers"))
    with SessionLocal() as session:
        if session.scalar(select(Organization.id).where(Organization.slug==data["slug"])):raise ValueError("این شناسه قبلاً استفاده شده است.")
        child=Organization(name=data["name"],slug=data["slug"],parent_id=ctx["organization_id"],credit_limit_irr=int(data["credit"]));session.add(child);session.flush();session.add(Closure(ancestor_id=child.id,descendant_id=child.id,depth=0))
        for edge in session.scalars(select(Closure).where(Closure.descendant_id==ctx["organization_id"])).all():session.add(Closure(ancestor_id=edge.ancestor_id,descendant_id=child.id,depth=edge.depth+1))
        session.add(Contract(parent_id=ctx["organization_id"],child_id=child.id,price_per_gib_irr=int(data["price"])));account(session,child.id)
        actor=session.scalar(select(Actor).where(Actor.telegram_id==int(data["telegram_id"])))
        if not actor:actor=Actor(telegram_id=int(data["telegram_id"]),display_name=data["name"]);session.add(actor);session.flush()
        session.add(Membership(organization_id=child.id,actor_id=actor.id,role="reseller_admin"));session.commit();return child.name

async def set_state(redis,chat_id,state):await redis.setex(f"bot-state:{chat_id}",STATE_TTL,json.dumps(state,ensure_ascii=False))
async def get_state(redis,chat_id):
    raw=await redis.get(f"bot-state:{chat_id}");return json.loads(raw) if raw else None
async def clear_state(redis,chat_id):await redis.delete(f"bot-state:{chat_id}")

async def begin_reseller(redis,chat_id):
    await set_state(redis,chat_id,{"flow":"reseller","step":"name","data":{}});await send(chat_id,"نام نماینده را ارسال کنید. برای لغو /cancel را بفرستید.")

async def continue_state(redis,chat_id,value,state):
    if value=="/cancel":await clear_state(redis,chat_id);await clear_state(redis,chat_id);role=await current_role(chat_id);await send(chat_id,"عملیات لغو شد.",menu_for(role));return;return
    if state["flow"]=="credit":
        amount=value.replace(",","").strip()
        if not amount.isdigit() or int(amount)<=0:await send(chat_id,"مبلغ معتبر به ریال ارسال کنید.");return
        ctx=await asyncio.to_thread(require_context,chat_id)
        with SessionLocal() as session:session.add(FundingRequest(organization_id=ctx["organization_id"],actor_id=ctx["actor_id"],amount_irr=int(amount)));session.commit()
        await clear_state(redis,chat_id);await send(chat_id,"درخواست افزایش اعتبار ثبت شد.",menu_for(await current_role(chat_id)));return
    data=state["data"];step=state["step"]
    if step=="name":
        if len(value)<2:await send(chat_id,"نام باید حداقل دو حرف باشد.");return
        data["name"]=value;state["step"]="slug";await set_state(redis,chat_id,state);await send(chat_id,"شناسه انگلیسی نماینده را ارسال کنید؛ مثال: reseller-north")
    elif step=="slug":
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,78}[a-z0-9]",value):await send(chat_id,"شناسه فقط باید شامل حروف کوچک انگلیسی، عدد و خط تیره باشد.");return
        data["slug"]=value;state["step"]="telegram_id";await set_state(redis,chat_id,state);await send(chat_id,"شناسه عددی تلگرام نماینده را ارسال کنید. نماینده می‌تواند با دستور /id آن را ببیند.")
    elif step=="telegram_id":
        if not value.isdigit():await send(chat_id,"شناسه تلگرام باید عددی باشد.");return
        data["telegram_id"]=value;state["step"]="price";await set_state(redis,chat_id,state);await send(chat_id,"قیمت هر گیگابایت را به ریال ارسال کنید.")
    elif step=="price":
        if not value.replace(",","").isdigit() or int(value.replace(",",""))<=0:await send(chat_id,"قیمت معتبر ارسال کنید.");return
        data["price"]=value.replace(",","");state["step"]="credit";await set_state(redis,chat_id,state);await send(chat_id,"سقف اعتبار اولیه را به ریال ارسال کنید؛ برای بدون اعتبار عدد 0 را بفرستید.")
    elif step=="credit":
        if not value.replace(",","").isdigit():await send(chat_id,"اعتبار معتبر ارسال کنید.");return
        data["credit"]=value.replace(",","")
        try:
            name=await asyncio.to_thread(create_reseller,chat_id,data);await clear_state(redis,chat_id);await send(chat_id,f"نماینده <b>{html.escape(name)}</b> ساخته و حساب تلگرام متصل شد.",menu_for(await current_role(chat_id)))
        except Exception as exc:await clear_state(redis,chat_id);await send(chat_id,f"ساخت نماینده انجام نشد: {html.escape(str(exc))}",menu_for(await current_role(chat_id)))

async def ensure_linked(chat_id):
    ctx=await asyncio.to_thread(context,chat_id)
    if ctx:return ctx
    if chat_id==root_id():await send(chat_id,"حساب مدیر هنوز راه‌اندازی نشده است. ابتدا راه‌اندازی اولیه را در پنل انجام دهید.",panel_button("راه‌اندازی اولیه"))
    else:await send(chat_id,f"حساب شما هنوز به نمایندگی متصل نشده است.\nشناسه عددی شما: <code>{chat_id}</code>")
    return None

async def current_role(chat_id):
    ctx=await asyncio.to_thread(require_context,chat_id)
    return ctx["role"]

async def open_section(chat_id,section,redis):
    """Render one menu section; the role decides both the entry and the refusal."""
    role=await current_role(chat_id)
    permission=SECTION_PERMISSION.get(section)
    if not permission or not can(role,permission):
        await send(chat_id,denial_fa(permission or "view_dashboard"),menu_for(role));return
    menu=menu_for(role)
    if section=="dashboard":await send(chat_id,await asyncio.to_thread(dashboard,chat_id,can(role,"manage_resellers")),menu)
    elif section=="servers":message,markup=await asyncio.to_thread(server_list,chat_id);await send(chat_id,message,markup)
    elif section=="resellers":await send(chat_id,await asyncio.to_thread(reseller_list,chat_id,can(role,"decide_funding")),menu)
    elif section=="new_reseller":await begin_reseller(redis,chat_id)
    elif section=="funding":message,markup=await asyncio.to_thread(funding_requests,chat_id);await send(chat_id,message,markup or menu)
    elif section=="transactions":await send(chat_id,await asyncio.to_thread(transactions,chat_id),menu)
    elif section=="credit":
        await set_state(redis,chat_id,{"flow":"credit","step":"amount","data":{}})
        await send(chat_id,"مبلغ درخواستی را به ریال ارسال کنید. برای لغو /cancel را بفرستید.")
    elif section=="approvals":message,markup=await asyncio.to_thread(approval_requests,chat_id);await send(chat_id,message,markup or menu)
    elif section=="system":
        access="، ".join(LABELS[name] for name in sections_for(role)) or "فقط مشاهده"
        await send(chat_id,f"نقش شما: <b>{html.escape(role_fa(role))}</b>\nشناسهٔ تلگرام: <code>{chat_id}</code>\nدسترسی‌ها: {html.escape(access)}",panel_button("وب‌اپ مدیریت"))
    elif section=="support":
        await send(chat_id,f"تیکت پشتیبانی برای مدیر مجموعه ارسال شد. شناسهٔ شما: <code>{chat_id}</code>\nبرای پیگیری، همین شناسه را نگه دارید.",menu)
    elif section=="webapp":await send(chat_id,"ورود به پنل:",panel_button("باز کردن پنل"))
    else:await send(chat_id,"گزینه معتبر را از منوی خود انتخاب کنید.",menu)

async def handle_message(redis,chat_id,value):
    value=(value or "").strip()
    if value=="/id":await send(chat_id,f"شناسه عددی تلگرام شما: <code>{chat_id}</code>");return
    state=await get_state(redis,chat_id)
    if state:await continue_state(redis,chat_id,value,state);return
    if not await ensure_linked(chat_id):return
    if value=="/start":
        role=await current_role(chat_id)
        await send(chat_id,"خوش آمدید. منوی اختصاصی نقش شما فعال شد.",menu_for(role))
        await send(chat_id,await asyncio.to_thread(dashboard,chat_id,role in ("system_admin","reseller_admin")),section_menu(role))
        return
    if value in ("/menu","منو") or value==LABELS["dashboard"]:await open_section(chat_id,"dashboard",redis);return
    if value in ALIASES:await open_section(chat_id,ALIASES[value] if value in ALIASES else "dashboard",redis);return
    await open_section(chat_id,label_to_section(value) or "dashboard",redis)

async def handle_callback(redis,chat_id,callback_id,data):
    try:
        parts=data.split("|")
        if parts[0]=="section" and len(parts)==2:
            await answer_callback(callback_id,"در حال بارگذاری…")
            if parts[1]=="menu":await send(chat_id,"بخش موردنظر را انتخاب کنید:",section_menu(await current_role(chat_id)))
            else:await open_section(chat_id,parts[1],redis)
        elif parts[0]=="nodes" and len(parts)==2:message,markup=await asyncio.to_thread(node_list,chat_id,parts[1]);await answer_callback(callback_id);await send(chat_id,message,markup)
        elif parts[0]=="node" and len(parts)==4:
            message=await asyncio.to_thread(node_action,chat_id,parts[1],parts[2],parts[3])
            await answer_callback(callback_id,message if parts[3]!="status" else "وضعیت ارسال شد")
            if parts[3]=="status":await send(chat_id,f"<b>وضعیت نود</b>{NL}<code>{message}</code>")
        elif parts[0] in ("fundok","fundno") and len(parts)==2:message=await asyncio.to_thread(decide_funding,chat_id,parts[1],parts[0]=="fundok");await answer_callback(callback_id,message);await send(chat_id,message,menu_for(await current_role(chat_id)))
        elif parts[0]=="appr" and len(parts)==3:message=await asyncio.to_thread(decide_approval,chat_id,parts[1],parts[2]=="ok");await answer_callback(callback_id,message);await send(chat_id,message,menu_for(await current_role(chat_id)))
        else:await answer_callback(callback_id,"دستور نامعتبر")
    except PermissionError as exc:
        # These messages are written for the user; never forward an unexpected exception text.
        print(f"callback {data}: {exc}",flush=True);await answer_callback(callback_id,str(exc)[:190])
    except Exception as exc:print(f"callback {data}: {exc}",flush=True);await answer_callback(callback_id,"عملیات انجام نشد")

async def main():
    redis=Redis.from_url(os.getenv("REDIS_URL","redis://redis:6379/0"),decode_responses=True)
    try:await redis.xgroup_create(STREAM,GROUP,id="0",mkstream=True)
    except Exception:pass
    consumer=os.getenv("HOSTNAME","telegram-1")
    while True:
        rows=await redis.xreadgroup(GROUP,consumer,{STREAM:">"},count=50,block=5000)
        for _,items in rows:
            for message_id,raw in items:
                try:
                    update=json.loads(raw["update"])
                    if update.get("callback_query"):
                        callback=update["callback_query"];chat_id=callback["message"]["chat"]["id"];await handle_callback(redis,chat_id,callback["id"],callback.get("data",""))
                    else:
                        message=update.get("message") or {};chat_id=(message.get("chat") or {}).get("id")
                        if chat_id:await handle_message(redis,chat_id,message.get("text",""))
                    await redis.xack(STREAM,GROUP,message_id)
                except Exception as exc:print(f"telegram message {message_id}: {exc}",flush=True)

if __name__=="__main__":asyncio.run(main())
