from __future__ import annotations
import hmac, os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, create_engine, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, UUID, insert as pg_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from .domain import cascade
from .migrate import run_migrations
from .rbac import can, denial_fa, normalize

def uid(): return str(uuid4())
def now(): return datetime.now(timezone.utc)
def env_int(name, default):
    try: return int(os.getenv(name, str(default)))
    except ValueError: return default
DATABASE_URL=os.getenv("DATABASE_URL","postgresql+psycopg://control:control@localhost:5432/control")
ENGINE=create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=int(os.getenv("DB_POOL_SIZE","20")),
    max_overflow=int(os.getenv("DB_MAX_OVERFLOW","40")),
    pool_timeout=int(os.getenv("DB_POOL_TIMEOUT","10")),
    pool_recycle=1800,
)
SessionLocal=sessionmaker(bind=ENGINE,expire_on_commit=False,autoflush=False)
class Base(DeclarativeBase): pass

class Organization(Base):
    __tablename__="organizations"
    id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid)
    parent_id:Mapped[str|None]=mapped_column(ForeignKey("organizations.id"),index=True)
    name:Mapped[str]=mapped_column(String(160)); slug:Mapped[str]=mapped_column(String(80),unique=True)
    status:Mapped[str]=mapped_column(String(24),default="active",server_default="active")
    credit_limit_irr:Mapped[int]=mapped_column(BigInteger,default=0,server_default="0")
    max_depth:Mapped[int]=mapped_column(Integer,default=5,server_default="5")
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,server_default=func.now())
class Closure(Base):
    __tablename__="organization_closure"
    ancestor_id:Mapped[str]=mapped_column(ForeignKey("organizations.id"),primary_key=True)
    descendant_id:Mapped[str]=mapped_column(ForeignKey("organizations.id"),primary_key=True)
    depth:Mapped[int]=mapped_column(Integer)
class Contract(Base):
    __tablename__="contracts"; __table_args__=(UniqueConstraint("parent_id","child_id"),)
    id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid)
    parent_id:Mapped[str]=mapped_column(ForeignKey("organizations.id")); child_id:Mapped[str]=mapped_column(ForeignKey("organizations.id"),unique=True)
    price_per_gib_irr:Mapped[int]=mapped_column(BigInteger); status:Mapped[str]=mapped_column(String(24),default="active",server_default="active")
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,server_default=func.now())
class Account(Base):
    __tablename__="accounts"; __table_args__=(UniqueConstraint("owner_key","code"),)
    id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid)
    owner_key:Mapped[str]=mapped_column(String(80),index=True); code:Mapped[str]=mapped_column(String(40),default="wallet",server_default="wallet")
    balance_irr:Mapped[int]=mapped_column(BigInteger,default=0,server_default="0")
    updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,onupdate=now,server_default=func.now())
class Transaction(Base):
    __tablename__="transactions"
    id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid)
    idempotency_key:Mapped[str]=mapped_column(String(180),unique=True,index=True)
    kind:Mapped[str]=mapped_column(String(40)); reference:Mapped[str]=mapped_column(String(180),default="",server_default="")
    actor_id:Mapped[str|None]=mapped_column(String(36))
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,server_default=func.now())
    entries:Mapped[list["Entry"]]=relationship(cascade="all,delete-orphan")
class Entry(Base):
    __tablename__="entries"
    id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid)
    transaction_id:Mapped[str]=mapped_column(ForeignKey("transactions.id"),index=True)
    account_id:Mapped[str]=mapped_column(ForeignKey("accounts.id"),index=True)
    side:Mapped[str]=mapped_column(String(6)); amount_irr:Mapped[int]=mapped_column(BigInteger)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,server_default=func.now())
class Panel(Base):
    __tablename__="panels"
    id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid)
    name:Mapped[str]=mapped_column(String(120)); base_url:Mapped[str]=mapped_column(String(500))
    api_key_ref:Mapped[str|None]=mapped_column(Text); owner_user_ref:Mapped[str|None]=mapped_column(Text); owner_pass_ref:Mapped[str|None]=mapped_column(Text)
    usage_coefficient:Mapped[Decimal]=mapped_column(Numeric(10,4),default=Decimal("1"),server_default="1.0000")
    verify_tls:Mapped[bool]=mapped_column(Boolean,default=True,server_default="true"); status:Mapped[str]=mapped_column(String(24),default="active",server_default="active")
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,server_default=func.now())
class PanelOwner(Base):
    __tablename__="panel_owners"
    panel_id:Mapped[str]=mapped_column(ForeignKey("panels.id"),primary_key=True)
    organization_id:Mapped[str]=mapped_column(ForeignKey("organizations.id"),index=True)
class Binding(Base):
    __tablename__="bindings"; __table_args__=(UniqueConstraint("panel_id","pg_admin_id"),)
    id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid)
    organization_id:Mapped[str]=mapped_column(ForeignKey("organizations.id"),unique=True)
    panel_id:Mapped[str]=mapped_column(ForeignKey("panels.id")); pg_admin_id:Mapped[int]=mapped_column(Integer)
    username:Mapped[str]=mapped_column(String(80)); status:Mapped[str]=mapped_column(String(24),default="active",server_default="active")
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,server_default=func.now())
class Checkpoint(Base):
    __tablename__="usage_checkpoints"
    binding_id:Mapped[str]=mapped_column(ForeignKey("bindings.id"),primary_key=True)
    lifetime_bytes:Mapped[int]=mapped_column(BigInteger); observed_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))
    anomaly_count:Mapped[int]=mapped_column(Integer,default=0,server_default="0")
class Usage(Base):
    __tablename__="usage_observations"
    id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid)
    binding_id:Mapped[str]=mapped_column(ForeignKey("bindings.id")); lifetime_bytes:Mapped[int]=mapped_column(BigInteger); delta_bytes:Mapped[int]=mapped_column(BigInteger)
    status:Mapped[str]=mapped_column(String(24)); observed_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))
class Outbox(Base):
    __tablename__="outbox"
    id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid); topic:Mapped[str]=mapped_column(String(80),index=True)
    aggregate_id:Mapped[str]=mapped_column(String(80)); payload:Mapped[str]=mapped_column(Text); status:Mapped[str]=mapped_column(String(24),default="pending",server_default="pending")
    attempts:Mapped[int]=mapped_column(Integer,default=0,server_default="0")
    next_attempt_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,server_default=func.now())
    claimed_by:Mapped[str|None]=mapped_column(String(80)); claimed_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    completed_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); last_error:Mapped[str|None]=mapped_column(Text)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,server_default=func.now())
class Actor(Base):
    __tablename__="actors"
    id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid)
    telegram_id:Mapped[int]=mapped_column(BigInteger,unique=True,index=True)
    display_name:Mapped[str]=mapped_column(String(160),default="",server_default="")
    status:Mapped[str]=mapped_column(String(24),default="active",server_default="active")
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,server_default=func.now())
class Membership(Base):
    __tablename__="memberships"; __table_args__=(UniqueConstraint("organization_id","actor_id"),)
    id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid)
    organization_id:Mapped[str]=mapped_column(ForeignKey("organizations.id"),index=True)
    actor_id:Mapped[str]=mapped_column(ForeignKey("actors.id"),index=True)
    role:Mapped[str]=mapped_column(String(40),default="reseller_admin",server_default="reseller_admin")
    status:Mapped[str]=mapped_column(String(24),default="active",server_default="active")
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,server_default=func.now())
class FundingRequest(Base):
    __tablename__="funding_requests"
    id:Mapped[str]=mapped_column(String(36),primary_key=True,default=uid)
    organization_id:Mapped[str]=mapped_column(ForeignKey("organizations.id"),index=True)
    actor_id:Mapped[str]=mapped_column(ForeignKey("actors.id"),index=True)
    amount_irr:Mapped[int]=mapped_column(BigInteger)
    status:Mapped[str]=mapped_column(String(24),default="pending",server_default="pending",index=True)
    decided_by:Mapped[str|None]=mapped_column(String(36)); decided_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,server_default=func.now())
class SystemSetting(Base):
    __tablename__="system_settings"
    key:Mapped[str]=mapped_column(String(100),primary_key=True)
    value:Mapped[str]=mapped_column(Text)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,server_default=func.now())
class AuditLog(Base):
    __tablename__="audit_logs"
    id:Mapped[str]=mapped_column(UUID(as_uuid=False),primary_key=True,default=uid)
    actor_id:Mapped[str|None]=mapped_column(String(36))
    organization_id:Mapped[str|None]=mapped_column(String(36),index=True)
    action:Mapped[str]=mapped_column(String(100))
    target_type:Mapped[str]=mapped_column(String(80)); target_id:Mapped[str]=mapped_column(String(100))
    details:Mapped[dict]=mapped_column("metadata",JSONB,default=dict,server_default="{}")
    request_id:Mapped[str|None]=mapped_column(String(64))
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,server_default=func.now())
class ApprovalRequest(Base):
    __tablename__="approval_requests"
    id:Mapped[str]=mapped_column(UUID(as_uuid=False),primary_key=True,default=uid)
    requester_id:Mapped[str]=mapped_column(String(36),index=True)
    approver_id:Mapped[str|None]=mapped_column(String(36))
    action:Mapped[str]=mapped_column(String(100))
    target_type:Mapped[str]=mapped_column(String(80)); target_id:Mapped[str]=mapped_column(String(100))
    payload:Mapped[dict]=mapped_column(JSONB,default=dict)
    status:Mapped[str]=mapped_column(String(24),default="pending",server_default="pending")
    expires_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))
    decided_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); decision_note:Mapped[str|None]=mapped_column(Text)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,server_default=func.now())

class UsageIn(BaseModel): binding_id:str; lifetime_bytes:int=Field(ge=0); observed_at:datetime

def db():
    s=SessionLocal()
    try: yield s
    finally: s.close()
def auth(x_control_key:str=Header(default="")):
    expected=os.getenv("CONTROL_API_KEY","")
    if len(expected)<32 or not hmac.compare_digest(expected,x_control_key): raise HTTPException(401,"invalid control key")
def setting(s:Session,key:str,default:str="")->str:
    return s.scalar(select(SystemSetting.value).where(SystemSetting.key==key)) or default
def put_setting(s:Session,key:str,value:str)->None:
    s.merge(SystemSetting(key=key,value=value))
def audit(s:Session,action:str,target_type:str,target_id:str,*,actor_id:str|None=None,organization_id:str|None=None,metadata:dict|None=None,request_id:str|None=None)->None:
    s.add(AuditLog(action=action,target_type=target_type,target_id=str(target_id),actor_id=actor_id,organization_id=organization_id,details=metadata or {},request_id=request_id))
def account(s:Session,owner:str)->Account:
    a=s.scalar(select(Account).where(Account.owner_key==owner,Account.code=="wallet"))
    if not a:
        s.execute(pg_insert(Account.__table__).values(id=uid(),owner_key=owner,code="wallet",balance_irr=0).on_conflict_do_nothing(index_elements=["owner_key","code"]))
        a=s.scalar(select(Account).where(Account.owner_key==owner,Account.code=="wallet"))
    return a
def balance(s:Session,org:str)->int:
    return int(account(s,org).balance_irr)
def enable_ledger_write(s:Session)->None:
    s.execute(text("SELECT set_config('control.ledger_write','on',true)"))
def validate_panel_url(url:str)->None:
    """A panel must be reachable over TLS without credentials smuggled into the URL."""
    from urllib.parse import urlsplit
    try:
        parsed=urlsplit(url)
    except ValueError as exc:
        raise HTTPException(422,"server URL is invalid") from exc
    if parsed.scheme!="https":raise HTTPException(422,"server URL must use HTTPS")
    if not parsed.hostname:raise HTTPException(422,"server URL must include a hostname")
    if parsed.username or parsed.password:raise HTTPException(422,"credentials are not allowed in the server URL")
def depth_of(s:Session,org_id:str)->int:
    return int(s.scalar(select(func.max(Closure.depth)).where(Closure.descendant_id==org_id)) or 0)
def effective_depth_limit(s:Session,parent:Organization)->int:
    root_id=s.scalar(select(Closure.ancestor_id).where(Closure.descendant_id==parent.id).order_by(Closure.depth.desc()).limit(1))
    root=s.get(Organization,root_id) if root_id else parent
    return min(env_int("MAX_TREE_DEPTH",5),int(root.max_depth))
def assert_depth_allowed(s:Session,parent:Organization)->None:
    limit=effective_depth_limit(s,parent)
    if depth_of(s,parent.id)+1>limit: raise HTTPException(409,f"maximum reseller depth {limit} reached")
def transfer(s:Session,source:str,target:str,amount:int,key:str,kind:str,ref:str,enforce=True,actor_id:str|None=None):
    if amount<=0:raise ValueError("amount must be positive")
    old=s.scalar(select(Transaction).where(Transaction.idempotency_key==key))
    if old:return old
    sa,ta=account(s,source),account(s,target)
    s.flush()
    ids=sorted({sa.id,ta.id})
    # Take the account row locks in a deterministic order so parallel transfers cannot deadlock.
    s.execute(select(Account.id).where(Account.id.in_(ids)).order_by(Account.id).with_for_update()).all()
    old=s.scalar(select(Transaction).where(Transaction.idempotency_key==key))
    if old:return old
    sb=int(s.scalar(select(Account.balance_irr).where(Account.id==sa.id)))
    if enforce and source!="SYSTEM":
        # The session keeps instances alive after commit, so the locked row must overwrite
        # whatever the identity map already holds before status is trusted.
        org=s.scalar(select(Organization).where(Organization.id==source).with_for_update().execution_options(populate_existing=True))
        if not org:raise ValueError("paying organization not found")
        if org.status!="active":raise ValueError("organization suspended")
        if sb+org.credit_limit_irr<amount:raise ValueError("insufficient credit")
    enable_ledger_write(s)
    # Relative updates: the balance is never computed in Python, so a lost update cannot occur.
    s.execute(update(Account).where(Account.id==sa.id).values(balance_irr=Account.balance_irr-amount).execution_options(synchronize_session=False))
    s.execute(update(Account).where(Account.id==ta.id).values(balance_irr=Account.balance_irr+amount).execution_options(synchronize_session=False))
    s.expire(sa);s.expire(ta)
    tx=Transaction(idempotency_key=key,kind=kind,reference=ref,actor_id=actor_id)
    tx.entries=[Entry(account_id=sa.id,side="debit",amount_irr=amount),Entry(account_id=ta.id,side="credit",amount_irr=amount)]
    s.add(tx);s.flush();return tx
def edges(s:Session,org_id:str):
    out=[];cur=s.get(Organization,org_id);seen=set()
    while cur and cur.parent_id:
        if cur.id in seen:raise ValueError("hierarchy cycle")
        seen.add(cur.id); c=s.scalar(select(Contract).where(Contract.child_id==cur.id,Contract.status=="active"))
        if not c:raise ValueError("active contract missing")
        out.append((cur.id,cur.parent_id,c.price_per_gib_irr));cur=s.get(Organization,cur.parent_id)
    return out
class BillingHold(ValueError):
    def __init__(self,binding_id,organization_id,amount_irr=0):
        super().__init__("insufficient credit; billing hold required")
        self.binding_id=binding_id;self.organization_id=organization_id;self.amount_irr=amount_irr

def record_hold(s:Session,binding_id:str,organization_id:str,amount_irr:int=0):
    from .outbox import enqueue
    return enqueue(s,"billing.hold_required",binding_id,{"organization_id":organization_id,"amount_irr":amount_irr})

RESET_CONFIRM_READINGS=env_int("USAGE_RESET_CONFIRM_READINGS",3)

def observe(s:Session,data:UsageIn):
    b=s.get(Binding,data.binding_id)
    if not b:raise ValueError("binding not found")
    cp=s.scalar(select(Checkpoint).where(Checkpoint.binding_id==b.id).with_for_update())
    if not cp:
        s.add(Checkpoint(binding_id=b.id,lifetime_bytes=data.lifetime_bytes,observed_at=data.observed_at));s.add(Usage(binding_id=b.id,lifetime_bytes=data.lifetime_bytes,delta_bytes=0,status="baseline",observed_at=data.observed_at));return []
    if data.lifetime_bytes<cp.lifetime_bytes:
        cp.anomaly_count+=1
        s.add(Usage(binding_id=b.id,lifetime_bytes=data.lifetime_bytes,delta_bytes=0,status="regression",observed_at=data.observed_at))
        if cp.anomaly_count>=RESET_CONFIRM_READINGS:
            cp.lifetime_bytes=data.lifetime_bytes;cp.observed_at=data.observed_at;cp.anomaly_count=0
            s.add(Usage(binding_id=b.id,lifetime_bytes=data.lifetime_bytes,delta_bytes=0,status="reset_baseline",observed_at=data.observed_at))
            from .outbox import enqueue
            enqueue(s,"usage.reset_detected",b.id,{"organization_id":b.organization_id,"lifetime_bytes":data.lifetime_bytes})
        return []
    delta=data.lifetime_bytes-cp.lifetime_bytes
    coefficient=Decimal(str(s.scalar(select(Panel.usage_coefficient).where(Panel.id==b.panel_id)) or 1))
    rows=cascade(delta,edges(s,b.organization_id),coefficient)
    pending=0
    try:
        for x in rows:
            transfer(s,x.child_id,x.parent_id,x.amount_irr,f"usage:{b.id}:{cp.lifetime_bytes}:{data.lifetime_bytes}:{x.child_id}","usage",b.id)
            pending+=x.amount_irr
    except ValueError as exc:
        if str(exc)=="insufficient credit":raise BillingHold(b.id,b.organization_id,pending) from exc
        raise
    cp.lifetime_bytes=data.lifetime_bytes;cp.observed_at=data.observed_at;cp.anomaly_count=0
    s.add(Usage(binding_id=b.id,lifetime_bytes=data.lifetime_bytes,delta_bytes=delta,status="accepted",observed_at=data.observed_at))
    return [x.__dict__ for x in rows]

@asynccontextmanager
async def lifespan(_app:FastAPI):
    Base.metadata.create_all(ENGINE)
    run_migrations(ENGINE)
    yield

from .telegram_auth import TelegramAuthError, verify_init_data

def web_identity(x_telegram_init_data:str=Header(default="")):
    try:return verify_init_data(x_telegram_init_data,os.environ.get("TELEGRAM_BOT_TOKEN",""),max_age_seconds=3600)
    except TelegramAuthError as exc:raise HTTPException(401,str(exc)) from exc

def pick_membership(s:Session,actor_id:str,telegram_id:int):
    root=os.getenv("ROOT_TELEGRAM_ID","")
    root_org=setting(s,"bootstrap_complete","")
    rows=s.scalars(select(Membership).where(Membership.actor_id==actor_id,Membership.status=="active").order_by(Membership.created_at,Membership.id)).all()
    if telegram_id==int(root) if root.isdigit() else 0:
        preferred=[m for m in rows if m.organization_id==root_org]
        if preferred:return preferred[0]
    return rows[0] if rows else None
def membership_for(s:Session,telegram_id:int):
    actor=s.scalar(select(Actor).where(Actor.telegram_id==telegram_id,Actor.status=="active"))
    if not actor:raise HTTPException(403,"Telegram account is not linked")
    membership=pick_membership(s,actor.id,telegram_id)
    if not membership:raise HTTPException(403,"active membership not found")
    return actor,membership,s.get(Organization,membership.organization_id)

def configured_root_id()->int|None:
    value=os.getenv("ROOT_TELEGRAM_ID","")
    return int(value) if value.isdigit() else None

def effective_role(telegram_id:int,membership_role:str|None)->str:
    """The root Telegram identity is the only source of the system_admin role."""
    root=configured_root_id()
    if root is not None and telegram_id==root:return "system_admin"
    return normalize(membership_role)

def role_for(s:Session,telegram_id:int)->str:
    _,membership,_=membership_for(s,telegram_id)
    return effective_role(telegram_id,membership.role)

def require_permission(permission:str):
    """FastAPI dependency resolving the caller's role server-side on every request."""
    def dependency(identity=Depends(web_identity),s:Session=Depends(db)):
        actor,membership,organization=membership_for(s,identity.user_id)
        role=effective_role(identity.user_id,membership.role)
        if not can(role,permission):raise HTTPException(403,denial_fa(permission))
        return actor,membership,organization,role
    return dependency
