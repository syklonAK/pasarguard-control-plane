import os
from datetime import timedelta

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .app import (
    Account, Actor, ApprovalRequest, Binding, Closure, Contract, FundingRequest, Membership,
    Organization, Panel, account, assert_depth_allowed, audit, balance, db, membership_for, now,
    transfer, uid, web_identity,
)
from .telegram_auth import TelegramAuthError, verify_init_data
from .v04_app import app

app.version = "0.8.0"

def configured_root_id():
    value = os.getenv("ROOT_TELEGRAM_ID", "")
    return int(value) if value.isdigit() else None

def require_root(identity):
    root = configured_root_id()
    if root is None:
        raise HTTPException(503, "Telegram administrator is not configured")
    if identity.user_id != root:
        raise HTTPException(403, "system administrator permission required")

@app.middleware("http")
async def protect_initial_bootstrap(request: Request, call_next):
    if request.method == "POST" and request.url.path == "/v1/onboarding/bootstrap":
        root = configured_root_id()
        if root is None:
            return JSONResponse({"detail": "ROOT_TELEGRAM_ID is not configured"}, status_code=503)
        try:
            identity = verify_init_data(request.headers.get("X-Telegram-Init-Data", ""), os.getenv("TELEGRAM_BOT_TOKEN", ""), max_age_seconds=3600)
        except TelegramAuthError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=401)
        if identity.user_id != root:
            return JSONResponse({"detail": "only the configured Telegram administrator can initialize this workspace"}, status_code=403)
    return await call_next(request)

class AdminResellerIn(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$")
    telegram_id: int = Field(gt=0)
    price_per_gib_irr: int = Field(gt=0)
    credit_limit_irr: int = Field(default=0, ge=0)

@app.get("/v1/webapp/capabilities")
def web_capabilities(identity=Depends(web_identity), s: Session = Depends(db)):
    _, membership, organization = membership_for(s, identity.user_id)
    is_root = identity.user_id == configured_root_id()
    can_manage = membership.role in ("reseller_admin", "operator")
    return {"is_system_admin": is_root, "role": membership.role, "organization_id": organization.id,
            "permissions": {"manage_servers": can_manage, "manage_resellers": can_manage, "review_funding": is_root,
                            "view_finance": membership.role in ("reseller_admin", "operator", "finance")}}

@app.get("/v1/webapp/admin/overview")
def admin_overview(identity=Depends(web_identity), s: Session = Depends(db)):
    require_root(identity); membership_for(s, identity.user_id)
    return {"organizations": int(s.scalar(select(func.count()).select_from(Organization)) or 0),
            "active_organizations": int(s.scalar(select(func.count()).select_from(Organization).where(Organization.status == "active")) or 0),
            "panels": int(s.scalar(select(func.count()).select_from(Panel).where(Panel.status == "active")) or 0),
            "actors": int(s.scalar(select(func.count()).select_from(Actor).where(Actor.status == "active")) or 0),
            "pending_funding": int(s.scalar(select(func.count()).select_from(FundingRequest).where(FundingRequest.status == "pending")) or 0),
            "bindings": int(s.scalar(select(func.count()).select_from(Binding).where(Binding.status == "active")) or 0)}

@app.get("/v1/webapp/admin/resellers")
def admin_resellers(identity=Depends(web_identity), s: Session = Depends(db)):
    require_root(identity); _, _, root_org = membership_for(s, identity.user_id)
    rows = s.scalars(select(Organization).where(Organization.parent_id == root_org.id).order_by(Organization.created_at.desc()).limit(200)).all()
    result = []
    for org in rows:
        wallet = s.scalar(select(Account).where(Account.owner_key == org.id, Account.code == "wallet"))
        actor = s.scalar(select(Actor).join(Membership, Membership.actor_id == Actor.id).where(Membership.organization_id == org.id, Membership.status == "active"))
        contract = s.scalar(select(Contract).where(Contract.child_id == org.id, Contract.status == "active"))
        result.append({"id": org.id, "name": org.name, "slug": org.slug, "status": org.status,
                       "telegram_id": actor.telegram_id if actor else None,
                       "balance_irr": int(wallet.balance_irr if wallet else 0),
                       "credit_limit_irr": int(org.credit_limit_irr),
                       "price_per_gib_irr": int(contract.price_per_gib_irr if contract else 0),
                       "created_at": org.created_at})
    return result

@app.post("/v1/webapp/admin/resellers")
def admin_create_reseller(x: AdminResellerIn, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, parent = membership_for(s, identity.user_id); actor_id = actor.id
    require_root(identity)
    assert_depth_allowed(s, parent)
    if s.scalar(select(Organization.id).where(Organization.slug == x.slug)):
        raise HTTPException(409, "slug already exists")
    target = s.scalar(select(Actor).where(Actor.telegram_id == x.telegram_id))
    if target and s.scalar(select(Membership.id).where(Membership.actor_id == target.id, Membership.status == "active")):
        raise HTTPException(409, "Telegram account is already linked")
    child = Organization(name=x.name, slug=x.slug, parent_id=parent.id, credit_limit_irr=x.credit_limit_irr)
    s.add(child); s.flush()
    s.add(Closure(ancestor_id=child.id, descendant_id=child.id, depth=0))
    for edge in s.scalars(select(Closure).where(Closure.descendant_id == parent.id)).all():
        s.add(Closure(ancestor_id=edge.ancestor_id, descendant_id=child.id, depth=edge.depth + 1))
    s.add(Contract(parent_id=parent.id, child_id=child.id, price_per_gib_irr=x.price_per_gib_irr))
    account(s, child.id)
    if not target:
        target = Actor(telegram_id=x.telegram_id, display_name=x.name)
        s.add(target); s.flush()
    else:
        target.status = "active"
        target.display_name = target.display_name or x.name
    s.add(Membership(organization_id=child.id, actor_id=target.id, role="reseller_admin"))
    audit(s, "reseller.create", "organization", child.id, actor_id=actor_id, organization_id=child.id,
          metadata={"parent_id": parent.id, "slug": child.slug, "telegram_id": target.telegram_id,
                    "price_per_gib_irr": x.price_per_gib_irr, "credit_limit_irr": x.credit_limit_irr})
    s.commit()
    return {"id": child.id, "name": child.name, "telegram_id": target.telegram_id}

@app.post("/v1/webapp/admin/resellers/{organization_id}/status")
def admin_set_reseller_status(organization_id: str, identity=Depends(web_identity), s: Session = Depends(db)):
    """Suspend or reactivate a direct reseller; settlement refuses suspended organizations."""
    actor, _, parent = membership_for(s, identity.user_id); actor_id = actor.id
    require_root(identity)
    child = s.get(Organization, organization_id)
    if not child or child.parent_id != parent.id:
        raise HTTPException(404, "reseller not found")
    child.status = "suspended" if child.status == "active" else "active"
    audit(s, "reseller.suspend" if child.status == "suspended" else "reseller.activate", "organization", child.id,
          actor_id=actor_id, organization_id=child.id)
    s.commit()
    return {"id": child.id, "status": child.status}

@app.get("/v1/webapp/admin/funding-requests")
def admin_funding_requests(identity=Depends(web_identity), s: Session = Depends(db)):
    require_root(identity); membership_for(s, identity.user_id)
    rows = s.execute(select(FundingRequest, Organization, Actor)
                     .join(Organization, Organization.id == FundingRequest.organization_id)
                     .join(Actor, Actor.id == FundingRequest.actor_id)
                     .where(FundingRequest.status == "pending").order_by(FundingRequest.created_at.asc()).limit(100)).all()
    return [{"id": request.id, "organization": organization.name, "telegram_id": actor.telegram_id,
             "amount_irr": request.amount_irr, "status": request.status, "created_at": request.created_at}
            for request, organization, actor in rows]

@app.post("/v1/webapp/admin/funding-requests/{request_id}/{decision}")
def admin_decide_funding(request_id: str, decision: str, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, root_org = membership_for(s, identity.user_id); actor_id = actor.id
    require_root(identity)
    if decision not in ("approve", "reject"):
        raise HTTPException(422, "decision must be approve or reject")
    request = s.scalar(select(FundingRequest).where(FundingRequest.id == request_id).with_for_update())
    if not request:
        raise HTTPException(404, "funding request not found")
    if request.status != "pending":
        raise HTTPException(409, "funding request is already processed")
    child = s.get(Organization, request.organization_id)
    if not child:
        raise HTTPException(404, "organization not found")
    # Only the direct parent of the requester may release the credit.
    if child.parent_id and child.parent_id != root_org.id:
        raise HTTPException(403, "this request belongs to another reseller branch")
    if decision == "approve":
        transfer(s, "SYSTEM", request.organization_id, request.amount_irr, f"funding-request:{request.id}", "fund", request.id, False, actor_id)
        request.status = "approved"
    else:
        request.status = "rejected"
    request.decided_by = actor_id
    request.decided_at = now()
    audit(s, f"funding_request.{decision}", "funding_request", request.id, actor_id=actor_id,
          organization_id=request.organization_id,
          metadata={"amount_irr": request.amount_irr, "decided_by": actor_id})
    s.commit()
    return {"id": request.id, "status": request.status}

APPROVAL_TTL_SECONDS = int(os.getenv("APPROVAL_TTL_SECONDS", "604800"))
ADJUST = "wallet.adjust"

class AdjustmentIn(BaseModel):
    organization_id: str
    amount_irr: int = Field(ne=0)
    note: str = Field(min_length=10, max_length=2000)

def approval_payload(approval):
    return {"id": approval.id, "action": approval.action, "target_type": approval.target_type,
            "target_id": approval.target_id, "status": approval.status, "payload": approval.payload,
            "requester_id": approval.requester_id, "approver_id": approval.approver_id,
            "decision_note": approval.decision_note, "expires_at": approval.expires_at,
            "decided_at": approval.decided_at, "created_at": approval.created_at}

def locked_pending_approval(s: Session, approval_id: str, now_value):
    approval = s.scalar(select(ApprovalRequest).where(ApprovalRequest.id == approval_id).with_for_update())
    if not approval:
        raise HTTPException(404, "approval request not found")
    if approval.status != "pending":
        raise HTTPException(409, "approval request is already decided")
    if approval.expires_at <= now_value:
        approval.status = "expired"
        approval.decided_at = now_value
        s.commit()
        raise HTTPException(410, "approval request expired")
    return approval

def apply_wallet_adjustment(s: Session, approval, actor_id: str):
    organization = s.get(Organization, approval.target_id)
    if not organization:
        raise HTTPException(404, "organization not found")
    amount = int(approval.payload["amount_irr"])
    # A correction may never leave an organization owing more than its agreed credit.
    try:
        if amount > 0:
            transfer(s, "SYSTEM", organization.id, amount, f"approval:{approval.id}", "adjustment", approval.id, False, actor_id)
        else:
            transfer(s, organization.id, "SYSTEM", -amount, f"approval:{approval.id}", "adjustment", approval.id, True, actor_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    audit(s, "adjustment.apply", "organization", organization.id, actor_id=actor_id, organization_id=organization.id,
          metadata={"amount_irr": amount, "note": approval.payload["note"], "approval_id": approval.id})

APPROVAL_APPLICATORS = {ADJUST: apply_wallet_adjustment}

@app.post("/v1/webapp/admin/adjustments")
def request_adjustment(x: AdjustmentIn, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, membership, organization = membership_for(s, identity.user_id)
    if membership.role not in ("reseller_admin", "operator", "finance"):
        raise HTTPException(403, "wallet adjustment permission required")
    target = s.get(Organization, x.organization_id)
    if not target:
        raise HTTPException(404, "organization not found")
    if organization.id != target.id and target.parent_id != organization.id:
        raise HTTPException(403, "adjustment is limited to your own subtree")
    if identity.user_id == configured_root_id():
        # The root administrator is the approver, so applying directly keeps the maker-checker rule meaningful.
        approval = ApprovalRequest(requester_id=actor.id, approver_id=actor.id, action=ADJUST, target_type="organization",
                                   target_id=target.id, payload={"amount_irr": x.amount_irr, "note": x.note},
                                   status="pending", expires_at=now() + timedelta(seconds=APPROVAL_TTL_SECONDS))
        s.add(approval); s.flush()
        apply_wallet_adjustment(s, approval, actor.id)
        approval.status = "executed"; approval.decided_at = now(); approval.decision_note = x.note
        s.commit()
        return {"id": approval.id, "status": approval.status, "balance_irr": balance(s, target.id)}
    approval = ApprovalRequest(requester_id=actor.id, action=ADJUST, target_type="organization", target_id=target.id,
                               payload={"amount_irr": x.amount_irr, "note": x.note},
                               expires_at=now() + timedelta(seconds=APPROVAL_TTL_SECONDS))
    s.add(approval)
    audit(s, "adjustment.request", "organization", target.id, actor_id=actor.id, organization_id=organization.id,
          metadata={"amount_irr": x.amount_irr, "note": x.note})
    s.commit()
    return {"id": approval.id, "status": approval.status}

@app.get("/v1/webapp/admin/approvals")
def list_approvals(identity=Depends(web_identity), s: Session = Depends(db)):
    require_root(identity); membership_for(s, identity.user_id)
    rows = s.scalars(select(ApprovalRequest).where(ApprovalRequest.status == "pending").order_by(ApprovalRequest.created_at).limit(200)).all()
    return [approval_payload(a) for a in rows]

@app.post("/v1/webapp/admin/approvals/{approval_id}/{decision}")
def decide_approval(approval_id: str, decision: str, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, _ = membership_for(s, identity.user_id)
    require_root(identity)
    if decision not in ("approve", "reject"):
        raise HTTPException(422, "decision must be approve or reject")
    approval = locked_pending_approval(s, approval_id, now())
    if decision == "reject":
        approval.status = "rejected"; approval.approver_id = actor.id
        approval.decided_at = now(); approval.decision_note = "rejected by system administrator"
    else:
        applicator = APPROVAL_APPLICATORS.get(approval.action)
        if not applicator:
            raise HTTPException(422, f"no applicator registered for {approval.action}")
        applicator(s, approval, actor.id)
        approval.status = "executed"; approval.approver_id = actor.id
        approval.decided_at = now(); approval.decision_note = "approved by system administrator"
    audit(s, f"approval.{decision}", "approval_request", approval.id, actor_id=actor.id,
          metadata={"action": approval.action, "target_id": approval.target_id, "payload": approval.payload})
    from .outbox import enqueue
    enqueue(s, "approval.granted", approval.id, {"action": approval.action, "status": approval.status})
    s.commit()
    return {"id": approval.id, "status": approval.status}
