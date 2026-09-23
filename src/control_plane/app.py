from __future__ import annotations
import hmac, json, os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, create_engine, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, UUID, insert as pg_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from .domain import cascade
from .migrate import run_migrations

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

class OrgIn(BaseModel):
    name:str=Field(min_length=2,max_length=160); slug:str=Field(pattern=r"^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$")
    parent_id:str|None=None; price_per_gib_irr:int|None=Field(default=None,ge=0); credit_limit_irr:int=Field(default=0,ge=0)
class FundIn(BaseModel): amount_irr:int=Field(gt=0); idempotency_key:str=Field(min_length=8,max_length=180)
class UsageIn(BaseModel): binding_id:str; lifetime_bytes:int=Field(ge=0); observed_at:datetime
class PanelIn(BaseModel):
    # Secrets are never accepted as plain references over HTTP; register a server through the
    # authenticated WebApp so it can be encrypted server-side.
    model_config=ConfigDict(extra="forbid")
    name:str=Field(min_length=2,max_length=120); base_url:str=Field(min_length=10,max_length=500)
    usage_coefficient:Decimal=Field(default=Decimal("1"),gt=Decimal("0"),le=Decimal("1000"))
class BindingIn(BaseModel): organization_id:str; panel_id:str; pg_admin_id:int; username:str

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
app=FastAPI(title="PasarGuard B2B Control Plane",version="0.8.0",lifespan=lifespan)
@app.get("/health")
def health():return {"status":"ok","time":now()}
@app.post("/v1/organizations",dependencies=[Depends(auth)])
def create_org(x:OrgIn,s:Session=Depends(db)):
    if s.scalar(select(Organization.id).where(Organization.slug==x.slug)):raise HTTPException(409,"slug exists")
    parent=s.get(Organization,x.parent_id) if x.parent_id else None
    if x.parent_id and not parent:raise HTTPException(404,"parent not found")
    if parent and x.price_per_gib_irr is None:raise HTTPException(422,"child price required")
    if parent:assert_depth_allowed(s,parent)
    o=Organization(name=x.name,slug=x.slug,parent_id=x.parent_id,credit_limit_irr=x.credit_limit_irr);s.add(o);s.flush();s.add(Closure(ancestor_id=o.id,descendant_id=o.id,depth=0))
    if parent:
        for e in s.scalars(select(Closure).where(Closure.descendant_id==parent.id)).all():s.add(Closure(ancestor_id=e.ancestor_id,descendant_id=o.id,depth=e.depth+1))
        s.add(Contract(parent_id=parent.id,child_id=o.id,price_per_gib_irr=x.price_per_gib_irr))
    account(s,o.id);audit(s,"organization.create","organization",o.id,organization_id=o.id,metadata={"slug":o.slug,"parent_id":o.parent_id});s.commit();return {"id":o.id,"name":o.name,"parent_id":o.parent_id}
@app.get("/v1/organizations",dependencies=[Depends(auth)])
def list_orgs(s:Session=Depends(db)):return [{"id":o.id,"name":o.name,"parent_id":o.parent_id,"status":o.status} for o in s.scalars(select(Organization)).all()]
@app.post("/v1/organizations/{org_id}/fund",dependencies=[Depends(auth)])
def fund(org_id:str,x:FundIn,s:Session=Depends(db)):
    if not s.get(Organization,org_id):raise HTTPException(404,"organization not found")
    tx=transfer(s,"SYSTEM",org_id,x.amount_irr,x.idempotency_key,"fund",org_id,False);audit(s,"wallet.fund","transaction",tx.id,organization_id=org_id,metadata={"amount_irr":x.amount_irr,"idempotency_key":x.idempotency_key});s.commit();return {"transaction_id":tx.id,"balance_irr":balance(s,org_id)}
@app.get("/v1/organizations/{org_id}/wallet",dependencies=[Depends(auth)])
def wallet(org_id:str,s:Session=Depends(db)):
    o=s.get(Organization,org_id)
    if not o:raise HTTPException(404,"organization not found")
    b=balance(s,org_id);return {"balance_irr":b,"credit_limit_irr":o.credit_limit_irr,"available_irr":b+o.credit_limit_irr}
@app.post("/v1/panels",dependencies=[Depends(auth)])
def panel(x:PanelIn,s:Session=Depends(db)):
    url=x.base_url.rstrip("/")
    validate_panel_url(url)
    p=Panel(name=x.name,base_url=url,usage_coefficient=x.usage_coefficient,verify_tls=True)
    s.add(p);audit(s,"panel.create","panel",p.id,metadata={"base_url":url,"usage_coefficient":str(x.usage_coefficient)});s.commit();return {"id":p.id,"name":p.name}
@app.post("/v1/bindings",dependencies=[Depends(auth)])
def binding(x:BindingIn,s:Session=Depends(db)):
    if not s.get(Organization,x.organization_id) or not s.get(Panel,x.panel_id):raise HTTPException(404,"org/panel not found")
    b=Binding(**x.model_dump());s.add(b);audit(s,"binding.create","binding",b.id,organization_id=x.organization_id,metadata={"panel_id":x.panel_id,"pg_admin_id":x.pg_admin_id});s.commit();return {"id":b.id}
@app.post("/v1/internal/usage",dependencies=[Depends(auth)])
def usage(x:UsageIn,s:Session=Depends(db)):
    try:r=observe(s,x);s.commit();return {"settlements":r}
    except BillingHold as e:
        s.rollback();record_hold(s,e.binding_id,e.organization_id,e.amount_irr);s.commit();raise HTTPException(402,str(e))
    except ValueError as e:s.rollback();raise HTTPException(409,str(e))

class UsageBatchIn(BaseModel):
    observations:list[UsageIn]=Field(min_length=1,max_length=1000)

@app.post("/v1/internal/usage/batch",dependencies=[Depends(auth)])
def usage_batch(x:UsageBatchIn,s:Session=Depends(db)):
    results=[];holds=[]
    for item in x.observations:
        try:results.append({"binding_id":item.binding_id,"settlements":observe(s,item)})
        except BillingHold as e:
            holds.append({"binding_id":e.binding_id,"organization_id":e.organization_id});record_hold(s,e.binding_id,e.organization_id,e.amount_irr)
    if holds and not results:
        s.commit();raise HTTPException(402,"billing hold recorded")
    s.commit()
    return {"processed":len(results),"holds":holds,"results":results}

from .telegram_auth import TelegramAuthError, verify_init_data

class ActorBindIn(BaseModel):
    telegram_id:int
    display_name:str=Field(default="",max_length=160)
    organization_id:str
    role:str=Field(default="reseller_admin",pattern=r"^(reseller_admin|operator|finance|viewer)$")
class FundingRequestIn(BaseModel): amount_irr:int=Field(gt=0,le=10_000_000_000_000)
class WebChildIn(BaseModel):
    name:str=Field(min_length=2,max_length=160);slug:str=Field(pattern=r"^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$")
    price_per_gib_irr:int=Field(gt=0);credit_limit_irr:int=Field(default=0,ge=0)

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

@app.post("/v1/admin/actor-bindings",dependencies=[Depends(auth)])
def bind_actor(x:ActorBindIn,s:Session=Depends(db)):
    if not s.get(Organization,x.organization_id):raise HTTPException(404,"organization not found")
    actor=s.scalar(select(Actor).where(Actor.telegram_id==x.telegram_id))
    if not actor:actor=Actor(telegram_id=x.telegram_id,display_name=x.display_name);s.add(actor);s.flush()
    m=s.scalar(select(Membership).where(Membership.organization_id==x.organization_id,Membership.actor_id==actor.id))
    if not m:s.add(Membership(organization_id=x.organization_id,actor_id=actor.id,role=x.role))
    audit(s,"actor.bind","actor",actor.id,organization_id=x.organization_id,metadata={"telegram_id":x.telegram_id,"role":x.role})
    s.commit();return {"actor_id":actor.id,"organization_id":x.organization_id}

@app.get("/v1/webapp/dashboard")
def web_dashboard(identity=Depends(web_identity),s:Session=Depends(db)):
    actor,m,o=membership_for(s,identity.user_id);wallet=balance(s,o.id)
    children=int(s.scalar(select(func.count()).select_from(Organization).where(Organization.parent_id==o.id)) or 0)
    binding=s.scalar(select(Binding).where(Binding.organization_id==o.id))
    cp=s.get(Checkpoint,binding.id) if binding else None
    return {"actor":{"name":actor.display_name,"role":m.role},"organization":{"id":o.id,"name":o.name,"status":o.status},"wallet":{"balance_irr":wallet,"credit_limit_irr":o.credit_limit_irr,"available_irr":wallet+o.credit_limit_irr},"usage":{"lifetime_bytes":cp.lifetime_bytes if cp else 0,"observed_at":cp.observed_at if cp else None},"children_count":children}
@app.get("/v1/webapp/children")
def web_children(identity=Depends(web_identity),s:Session=Depends(db)):
    _,_,o=membership_for(s,identity.user_id)
    rows=s.scalars(select(Organization).where(Organization.parent_id==o.id).order_by(Organization.created_at.desc()).limit(100)).all()
    return [{"id":x.id,"name":x.name,"status":x.status,"balance_irr":balance(s,x.id),"credit_limit_irr":x.credit_limit_irr} for x in rows]
@app.post("/v1/webapp/children")
def web_create_child(x:WebChildIn,identity=Depends(web_identity),s:Session=Depends(db)):
    actor,m,parent=membership_for(s,identity.user_id)
    if m.role not in ("reseller_admin","operator"):raise HTTPException(403,"role cannot create child")
    if s.scalar(select(Organization.id).where(Organization.slug==x.slug)):raise HTTPException(409,"slug exists")
    assert_depth_allowed(s,parent)
    child=Organization(name=x.name,slug=x.slug,parent_id=parent.id,credit_limit_irr=x.credit_limit_irr);s.add(child);s.flush();s.add(Closure(ancestor_id=child.id,descendant_id=child.id,depth=0))
    for edge in s.scalars(select(Closure).where(Closure.descendant_id==parent.id)).all():s.add(Closure(ancestor_id=edge.ancestor_id,descendant_id=child.id,depth=edge.depth+1))
    s.add(Contract(parent_id=parent.id,child_id=child.id,price_per_gib_irr=x.price_per_gib_irr));account(s,child.id)
    audit(s,"organization.create","organization",child.id,actor_id=actor.id,organization_id=parent.id,metadata={"slug":child.slug,"price_per_gib_irr":x.price_per_gib_irr})
    s.commit();return {"id":child.id,"name":child.name}
@app.get("/v1/webapp/transactions")
def web_transactions(identity=Depends(web_identity),s:Session=Depends(db)):
    _,_,o=membership_for(s,identity.user_id);a=account(s,o.id)
    rows=s.execute(select(Transaction,Entry).join(Entry,Entry.transaction_id==Transaction.id).where(Entry.account_id==a.id).order_by(Transaction.created_at.desc()).limit(50)).all()
    return [{"id":tx.id,"kind":tx.kind,"amount_irr":entry.amount_irr,"side":entry.side,"created_at":tx.created_at} for tx,entry in rows]
@app.post("/v1/webapp/funding-requests")
def web_funding_request(x:FundingRequestIn,identity=Depends(web_identity),s:Session=Depends(db)):
    actor,_,o=membership_for(s,identity.user_id);req=FundingRequest(organization_id=o.id,actor_id=actor.id,amount_irr=x.amount_irr);s.add(req)
    audit(s,"funding_request.create","funding_request",req.id,actor_id=actor.id,organization_id=o.id,metadata={"amount_irr":x.amount_irr})
    s.commit();return {"id":req.id,"status":req.status}
