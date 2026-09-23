"""Machine-to-machine control API, authenticated with the shared ``X-Control-Key``."""
from __future__ import annotations
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from .app import (
    Actor, BillingHold, Binding, Closure, Contract, Membership, Organization, Panel, UsageIn,
    account, assert_depth_allowed, audit, auth, balance, db, observe, record_hold, transfer,
    validate_panel_url,
)
from .rbac import ASSIGNABLE_ROLES

router = APIRouter(dependencies=[Depends(auth)])

class OrgIn(BaseModel):
    name:str=Field(min_length=2,max_length=160); slug:str=Field(pattern=r"^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$")
    parent_id:str|None=None; price_per_gib_irr:int|None=Field(default=None,ge=0); credit_limit_irr:int=Field(default=0,ge=0)
class FundIn(BaseModel): amount_irr:int=Field(gt=0); idempotency_key:str=Field(min_length=8,max_length=180)
class PanelIn(BaseModel):
    # Secrets are never accepted as plain references over HTTP; register a server through the
    # authenticated WebApp so it can be encrypted server-side.
    model_config=ConfigDict(extra="forbid")
    name:str=Field(min_length=2,max_length=120); base_url:str=Field(min_length=10,max_length=500)
    usage_coefficient:Decimal=Field(default=Decimal("1"),gt=Decimal("0"),le=Decimal("1000"))
class BindingIn(BaseModel): organization_id:str; panel_id:str; pg_admin_id:int; username:str
class ActorBindIn(BaseModel):
    telegram_id:int
    display_name:str=Field(default="",max_length=160)
    organization_id:str
    role:str=Field(default="reseller_admin",pattern=r"^(%s)$"%"|".join(ASSIGNABLE_ROLES))
class UsageBatchIn(BaseModel):
    observations:list[UsageIn]=Field(min_length=1,max_length=1000)

@router.post("/v1/organizations")
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

@router.get("/v1/organizations")
def list_orgs(s:Session=Depends(db)):return [{"id":o.id,"name":o.name,"parent_id":o.parent_id,"status":o.status} for o in s.scalars(select(Organization)).all()]

@router.post("/v1/organizations/{org_id}/fund")
def fund(org_id:str,x:FundIn,s:Session=Depends(db)):
    if not s.get(Organization,org_id):raise HTTPException(404,"organization not found")
    tx=transfer(s,"SYSTEM",org_id,x.amount_irr,x.idempotency_key,"fund",org_id,False);audit(s,"wallet.fund","transaction",tx.id,organization_id=org_id,metadata={"amount_irr":x.amount_irr,"idempotency_key":x.idempotency_key});s.commit();return {"transaction_id":tx.id,"balance_irr":balance(s,org_id)}

@router.get("/v1/organizations/{org_id}/wallet")
def wallet(org_id:str,s:Session=Depends(db)):
    o=s.get(Organization,org_id)
    if not o:raise HTTPException(404,"organization not found")
    b=balance(s,org_id);return {"balance_irr":b,"credit_limit_irr":o.credit_limit_irr,"available_irr":b+o.credit_limit_irr}

@router.post("/v1/panels")
def panel(x:PanelIn,s:Session=Depends(db)):
    url=x.base_url.rstrip("/")
    validate_panel_url(url)
    p=Panel(name=x.name,base_url=url,usage_coefficient=x.usage_coefficient,verify_tls=True)
    s.add(p);audit(s,"panel.create","panel",p.id,metadata={"base_url":url,"usage_coefficient":str(x.usage_coefficient)});s.commit();return {"id":p.id,"name":p.name}

@router.post("/v1/bindings")
def binding(x:BindingIn,s:Session=Depends(db)):
    if not s.get(Organization,x.organization_id) or not s.get(Panel,x.panel_id):raise HTTPException(404,"org/panel not found")
    b=Binding(**x.model_dump());s.add(b);audit(s,"binding.create","binding",b.id,organization_id=x.organization_id,metadata={"panel_id":x.panel_id,"pg_admin_id":x.pg_admin_id});s.commit();return {"id":b.id}

@router.post("/v1/admin/actor-bindings")
def bind_actor(x:ActorBindIn,s:Session=Depends(db)):
    if not s.get(Organization,x.organization_id):raise HTTPException(404,"organization not found")
    actor=s.scalar(select(Actor).where(Actor.telegram_id==x.telegram_id))
    if not actor:actor=Actor(telegram_id=x.telegram_id,display_name=x.display_name);s.add(actor);s.flush()
    m=s.scalar(select(Membership).where(Membership.organization_id==x.organization_id,Membership.actor_id==actor.id))
    if not m:s.add(Membership(organization_id=x.organization_id,actor_id=actor.id,role=x.role))
    audit(s,"actor.bind","actor",actor.id,organization_id=x.organization_id,metadata={"telegram_id":x.telegram_id,"role":x.role})
    s.commit();return {"actor_id":actor.id,"organization_id":x.organization_id}

@router.post("/v1/internal/usage")
def usage(x:UsageIn,s:Session=Depends(db)):
    try:r=observe(s,x);s.commit();return {"settlements":r}
    except BillingHold as e:
        s.rollback();record_hold(s,e.binding_id,e.organization_id,e.amount_irr);s.commit();raise HTTPException(402,str(e))
    except ValueError as e:s.rollback();raise HTTPException(409,str(e))

@router.post("/v1/internal/usage/batch")
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
