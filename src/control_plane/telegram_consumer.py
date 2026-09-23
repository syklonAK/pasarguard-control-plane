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
    Account, Actor, Binding, Checkpoint, Closure, Contract, Entry, FundingRequest,
    Membership, Organization, Panel, PanelOwner, SessionLocal, Transaction, account, transfer,
)
from .v04_app import _unwrap, panel_operation

STREAM = "telegram_updates"
GROUP = "telegram-workers"
STATE_TTL = 900

ADMIN_MENU = {"keyboard":[[{"text":"🏠 داشبورد مدیریت"},{"text":"🖥 مدیریت سرورها"}],[{"text":"👥 مدیریت نمایندگان"},{"text":"➕ نماینده جدید"}],[{"text":"💰 درخواست‌های مالی"},{"text":"📊 گزارش مالی"}],[{"text":"⚙️ مدیریت سیستم"},{"text":"🌐 پنل پیشرفته"}]],"resize_keyboard":True,"is_persistent":True}
RESELLER_MENU = {"keyboard":[[{"text":"🏠 حساب من"},{"text":"💳 کیف پول"}],[{"text":"📈 مصرف من"},{"text":"👥 زیرمجموعه‌ها"}],[{"text":"➕ درخواست اعتبار"},{"text":"📜 تراکنش‌ها"}],[{"text":"🛟 پشتیبانی"},{"text":"🌐 پنل کاربری"}]],"resize_keyboard":True,"is_persistent":True}

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
        return {"actor_id":actor.id,"organization_id":organization.id,"organization_name":organization.name,"role":membership.role}

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
    balance=int(wallet.balance_irr if wallet else 0);credit=int(organization.credit_limit_irr);usage=int(checkpoint.lifetime_bytes if checkpoint else 0)/1073741824
    title="داشبورد مدیریت" if admin else html.escape(organization.name)
    lines=[f"<b>{title}</b>","",f"موجودی: <b>{money(balance)}</b>",f"اعتبار: <b>{money(credit)}</b>",f"قابل استفاده: <b>{money(balance+credit)}</b>",f"مصرف: <b>{usage:,.2f} GiB</b>",f"زیرمجموعه مستقیم: <b>{direct_children}</b>"]
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
    for organization,balance,telegram_id in output:
        telegram_text=f" — <code>{telegram_id}</code>" if telegram_id else " — بدون اتصال تلگرام"
        lines.append(f"• <b>{html.escape(organization.name)}</b> — {money(balance)}{telegram_text}")
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

def funding_requests(chat_id):
    require_context(chat_id)
    if chat_id!=root_id():raise PermissionError("admin only")
    with SessionLocal() as session:
        rows=session.execute(select(FundingRequest,Organization).join(Organization,Organization.id==FundingRequest.organization_id).where(FundingRequest.status=="pending").order_by(FundingRequest.created_at).limit(20)).all()
    if not rows:return "درخواست مالی بازی وجود ندارد.",None
    lines=["<b>درخواست‌های افزایش اعتبار</b>"];keyboard=[]
    for request,organization in rows:
        lines.append(f"• {html.escape(organization.name)} — <b>{money(request.amount_irr)}</b>")
        keyboard.append([{"text":f"✅ تایید {organization.name[:18]}","callback_data":f"fundok|{request.id}"},{"text":"❌ رد","callback_data":f"fundno|{request.id}"}])
    return "\n".join(lines),{"inline_keyboard":keyboard}

def decide_funding(chat_id,request_id,approve):
    require_context(chat_id)
    if chat_id!=root_id():raise PermissionError("admin only")
    with SessionLocal() as session:
        request=session.scalar(select(FundingRequest).where(FundingRequest.id==request_id).with_for_update())
        if not request or request.status!="pending":return "این درخواست قبلاً بررسی شده است."
        if approve:
            transfer(session,"SYSTEM",request.organization_id,request.amount_irr,f"funding-request:{request.id}","fund",request.id,False);request.status="approved"
        else:request.status="rejected"
        session.commit()
    return "درخواست تایید و کیف پول شارژ شد." if approve else "درخواست رد شد."

def create_reseller(chat_id,data):
    ctx=require_context(chat_id)
    if chat_id!=root_id() and ctx["role"] not in ("reseller_admin","operator"):raise PermissionError("not allowed")
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
    if value=="/cancel":await clear_state(redis,chat_id);await send(chat_id,"عملیات لغو شد.",ADMIN_MENU if chat_id==root_id() else RESELLER_MENU);return
    if state["flow"]=="credit":
        amount=value.replace(",","").strip()
        if not amount.isdigit() or int(amount)<=0:await send(chat_id,"مبلغ معتبر به ریال ارسال کنید.");return
        ctx=await asyncio.to_thread(require_context,chat_id)
        with SessionLocal() as session:session.add(FundingRequest(organization_id=ctx["organization_id"],actor_id=ctx["actor_id"],amount_irr=int(amount)));session.commit()
        await clear_state(redis,chat_id);await send(chat_id,"درخواست افزایش اعتبار ثبت شد.",RESELLER_MENU);return
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
            name=await asyncio.to_thread(create_reseller,chat_id,data);await clear_state(redis,chat_id);await send(chat_id,f"نماینده <b>{html.escape(name)}</b> ساخته و حساب تلگرام متصل شد.",ADMIN_MENU if chat_id==root_id() else RESELLER_MENU)
        except Exception as exc:await clear_state(redis,chat_id);await send(chat_id,f"ساخت نماینده انجام نشد: {html.escape(str(exc))}",ADMIN_MENU if chat_id==root_id() else RESELLER_MENU)

async def ensure_linked(chat_id):
    ctx=await asyncio.to_thread(context,chat_id)
    if ctx:return ctx
    if chat_id==root_id():await send(chat_id,"حساب مدیر هنوز راه‌اندازی نشده است. ابتدا راه‌اندازی اولیه را در پنل انجام دهید.",panel_button("راه‌اندازی اولیه"))
    else:await send(chat_id,f"حساب شما هنوز به نمایندگی متصل نشده است.\nشناسه عددی شما: <code>{chat_id}</code>")
    return None

async def handle_message(redis,chat_id,value):
    value=(value or "").strip()
    if value=="/id":await send(chat_id,f"شناسه عددی تلگرام شما: <code>{chat_id}</code>");return
    state=await get_state(redis,chat_id)
    if state:await continue_state(redis,chat_id,value,state);return
    if value.startswith("/start"):
        menu=ADMIN_MENU if chat_id==root_id() else RESELLER_MENU;await send(chat_id,"خوش آمدید. منوی اختصاصی حساب شما فعال شد.",menu)
        if await ensure_linked(chat_id):await send(chat_id,await asyncio.to_thread(dashboard,chat_id,chat_id==root_id()),menu)
        return
    if not await ensure_linked(chat_id):return
    admin=chat_id==root_id()
    if admin and value=="🏠 داشبورد مدیریت":await send(chat_id,await asyncio.to_thread(dashboard,chat_id,True),ADMIN_MENU)
    elif admin and value=="🖥 مدیریت سرورها":message,markup=await asyncio.to_thread(server_list,chat_id);await send(chat_id,message,markup)
    elif admin and value=="👥 مدیریت نمایندگان":await send(chat_id,await asyncio.to_thread(reseller_list,chat_id,True),ADMIN_MENU)
    elif admin and value=="➕ نماینده جدید":await begin_reseller(redis,chat_id)
    elif admin and value=="💰 درخواست‌های مالی":message,markup=await asyncio.to_thread(funding_requests,chat_id);await send(chat_id,message,markup or ADMIN_MENU)
    elif admin and value=="📊 گزارش مالی":await send(chat_id,await asyncio.to_thread(transactions,chat_id),ADMIN_MENU)
    elif admin and value=="⚙️ مدیریت سیستم":await send(chat_id,f"شناسه مدیر: <code>{chat_id}</code>\nبرای مدیریت پیشرفته و ثبت امن کلید سرورها وارد پنل شوید.",panel_button("پنل مدیریت"))
    elif admin and value=="🌐 پنل پیشرفته":await send(chat_id,"ورود به پنل پیشرفته:",panel_button("باز کردن پنل"))
    elif not admin and value in ("🏠 حساب من","💳 کیف پول","📈 مصرف من"):await send(chat_id,await asyncio.to_thread(dashboard,chat_id,False),RESELLER_MENU)
    elif not admin and value=="👥 زیرمجموعه‌ها":await send(chat_id,await asyncio.to_thread(reseller_list,chat_id,False),RESELLER_MENU)
    elif not admin and value=="➕ درخواست اعتبار":await set_state(redis,chat_id,{"flow":"credit","step":"amount","data":{}});await send(chat_id,"مبلغ درخواستی را به ریال ارسال کنید. برای لغو /cancel را بفرستید.")
    elif not admin and value=="📜 تراکنش‌ها":await send(chat_id,await asyncio.to_thread(transactions,chat_id),RESELLER_MENU)
    elif not admin and value=="🛟 پشتیبانی":await send(chat_id,f"برای پشتیبانی با مدیر مجموعه تماس بگیرید. شناسه شما: <code>{chat_id}</code>",RESELLER_MENU)
    elif not admin and value=="🌐 پنل کاربری":await send(chat_id,"ورود به پنل کاربری:",panel_button("باز کردن پنل"))
    else:await send(chat_id,"گزینه معتبر را از منوی اختصاصی خود انتخاب کنید.",ADMIN_MENU if admin else RESELLER_MENU)

async def handle_callback(chat_id,callback_id,data):
    try:
        parts=data.split("|")
        if parts[0]=="nodes" and len(parts)==2:message,markup=await asyncio.to_thread(node_list,chat_id,parts[1]);await answer_callback(callback_id);await send(chat_id,message,markup)
        elif parts[0]=="node" and len(parts)==4:
            message=await asyncio.to_thread(node_action,chat_id,parts[1],parts[2],parts[3])
            await answer_callback(callback_id,message if parts[3]!="status" else "وضعیت ارسال شد")
            if parts[3]=="status":await send(chat_id,f"<b>وضعیت نود</b>\n<code>{message}</code>")
        elif parts[0] in ("fundok","fundno") and len(parts)==2:message=await asyncio.to_thread(decide_funding,chat_id,parts[1],parts[0]=="fundok");await answer_callback(callback_id,message);await send(chat_id,message,ADMIN_MENU)
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
                        callback=update["callback_query"];chat_id=callback["message"]["chat"]["id"];await handle_callback(chat_id,callback["id"],callback.get("data",""))
                    else:
                        message=update.get("message") or {};chat_id=(message.get("chat") or {}).get("id")
                        if chat_id:await handle_message(redis,chat_id,message.get("text",""))
                    await redis.xack(STREAM,GROUP,message_id)
                except Exception as exc:print(f"telegram message {message_id}: {exc}",flush=True)

if __name__=="__main__":asyncio.run(main())
