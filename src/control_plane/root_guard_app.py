import os
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from .app import Account, Actor, Binding, Closure, Contract, FundingRequest, Membership, Organization, Panel, account, db, membership_for, transfer, web_identity
from .telegram_auth import TelegramAuthError, verify_init_data
from .v04_app import app
app.version="0.7.0"
def configured_root_id():
 value=os.getenv("ROOT_TELEGRAM_ID","");return int(value) if value.isdigit() else None
def require_root(identity):
 root=configured_root_id()
 if root is None:raise HTTPException(503,"Telegram administrator is not configured")
 if identity.user_id!=root:raise HTTPException(403,"system administrator permission required")
@app.middleware("http")
async def protect_initial_bootstrap(request:Request,call_next):
 if request.method=="POST" and request.url.path=="/v1/onboarding/bootstrap":
  root=configured_root_id()
  if root is None:return JSONResponse({"detail":"ROOT_TELEGRAM_ID is not configured"},status_code=503)
  try:identity=verify_init_data(request.headers.get("X-Telegram-Init-Data",""),os.getenv("TELEGRAM_BOT_TOKEN",""),max_age_seconds=3600)
  except TelegramAuthError as exc:return JSONResponse({"detail":str(exc)},status_code=401)
  if identity.user_id!=root:return JSONResponse({"detail":"only the configured Telegram administrator can initialize this workspace"},status_code=403)
 return await call_next(request)
class AdminResellerIn(BaseModel):
 name:str=Field(min_length=2,max_length=160);slug:str=Field(pattern=r"^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$");telegram_id:int=Field(gt=0);price_per_gib_irr:int=Field(gt=0);credit_limit_irr:int=Field(default=0,ge=0)
@app.get("/v1/webapp/capabilities")
def web_capabilities(identity=Depends(web_identity),s:Session=Depends(db)):
 _,membership,organization=membership_for(s,identity.user_id);is_root=identity.user_id==configured_root_id();can_manage=membership.role in ("reseller_admin","operator")
 return {"is_system_admin":is_root,"role":membership.role,"organization_id":organization.id,"permissions":{"manage_servers":can_manage,"manage_resellers":can_manage,"review_funding":is_root,"view_finance":membership.role in ("reseller_admin","operator","finance")}}
@app.get("/v1/webapp/admin/overview")
def admin_overview(identity=Depends(web_identity),s:Session=Depends(db)):
 require_root(identity);membership_for(s,identity.user_id)
 return {"organizations":int(s.scalar(select(func.count()).select_from(Organization)) or 0),"active_organizations":int(s.scalar(select(func.count()).select_from(Organization).where(Organization.status=="active")) or 0),"panels":int(s.scalar(select(func.count()).select_from(Panel).where(Panel.status=="active")) or 0),"actors":int(s.scalar(select(func.count()).select_from(Actor).where(Actor.status=="active")) or 0),"pending_funding":int(s.scalar(select(func.count()).select_from(FundingRequest).where(FundingRequest.status=="pending")) or 0),"bindings":int(s.scalar(select(func.count()).select_from(Binding).where(Binding.status=="active")) or 0)}
@app.get("/v1/webapp/admin/resellers")
def admin_resellers(identity=Depends(web_identity),s:Session=Depends(db)):
 require_root(identity);_,_,root_org=membership_for(s,identity.user_id);rows=s.scalars(select(Organization).where(Organization.parent_id==root_org.id).order_by(Organization.created_at.desc()).limit(200)).all();result=[]
 for org in rows:
  wallet=s.scalar(select(Account).where(Account.owner_key==org.id,Account.code=="wallet"));actor=s.scalar(select(Actor).join(Membership,Membership.actor_id==Actor.id).where(Membership.organization_id==org.id,Membership.status=="active"));contract=s.scalar(select(Contract).where(Contract.child_id==org.id,Contract.status=="active"))
  result.append({"id":org.id,"name":org.name,"slug":org.slug,"status":org.status,"telegram_id":actor.telegram_id if actor else None,"balance_irr":int(wallet.balance_irr if wallet else 0),"credit_limit_irr":int(org.credit_limit_irr),"price_per_gib_irr":int(contract.price_per_gib_irr if contract else 0),"created_at":org.created_at})
 return result
@app.post("/v1/webapp/admin/resellers")
def admin_create_reseller(x:AdminResellerIn,identity=Depends(web_identity),s:Session=Depends(db)):
 require_root(identity);_,_,parent=membership_for(s,identity.user_id)
 if s.scalar(select(Organization.id).where(Organization.slug==x.slug)):raise HTTPException(409,"slug already exists")
 existing_actor=s.scalar(select(Actor).where(Actor.telegram_id==x.telegram_id))
 if existing_actor and s.scalar(select(Membership.id).where(Membership.actor_id==existing_actor.id,Membership.status=="active")):raise HTTPException(409,"Telegram account is already linked")
 child=Organization(name=x.name,slug=x.slug,parent_id=parent.id,credit_limit_irr=x.credit_limit_irr);s.add(child);s.flush();s.add(Closure(ancestor_id=child.id,descendant_id=child.id,depth=0))
 for edge in s.scalars(select(Closure).where(Closure.descendant_id==parent.id)).all():s.add(Closure(ancestor_id=edge.ancestor_id,descendant_id=child.id,depth=edge.depth+1))
 s.add(Contract(parent_id=parent.id,child_id=child.id,price_per_gib_irr=x.price_per_gib_irr));account(s,child.id);actor=existing_actor or Actor(telegram_id=x.telegram_id,display_name=x.name)
 if not existing_actor:s.add(actor);s.flush()
 s.add(Membership(organization_id=child.id,actor_id=actor.id,role="reseller_admin"));s.commit();return {"id":child.id,"name":child.name,"telegram_id":actor.telegram_id}
@app.get("/v1/webapp/admin/funding-requests")
def admin_funding_requests(identity=Depends(web_identity),s:Session=Depends(db)):
 require_root(identity);membership_for(s,identity.user_id);rows=s.execute(select(FundingRequest,Organization,Actor).join(Organization,Organization.id==FundingRequest.organization_id).join(Actor,Actor.id==FundingRequest.actor_id).where(FundingRequest.status=="pending").order_by(FundingRequest.created_at.asc()).limit(100)).all()
 return [{"id":request.id,"organization":organization.name,"telegram_id":actor.telegram_id,"amount_irr":request.amount_irr,"status":request.status,"created_at":request.created_at} for request,organization,actor in rows]
@app.post("/v1/webapp/admin/funding-requests/{request_id}/{decision}")
def admin_decide_funding(request_id:str,decision:str,identity=Depends(web_identity),s:Session=Depends(db)):
 require_root(identity);membership_for(s,identity.user_id)
 if decision not in ("approve","reject"):raise HTTPException(422,"decision must be approve or reject")
 request=s.scalar(select(FundingRequest).where(FundingRequest.id==request_id).with_for_update())
 if not request:raise HTTPException(404,"funding request not found")
 if request.status!="pending":raise HTTPException(409,"funding request is already processed")
 if decision=="approve":transfer(s,"SYSTEM",request.organization_id,request.amount_irr,f"funding-request:{request.id}","fund",request.id,False);request.status="approved"
 else:request.status="rejected"
 s.commit();return {"id":request.id,"status":request.status}
