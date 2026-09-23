"""Everything a human sees: the Telegram WebApp API and the root-administrator surface.

Roles are resolved here on every request from the active membership row (``app.effective_role``
plus the matrix in :mod:`control_plane.rbac`); the browser and the bot only render what this
module is willing to accept.
"""
from __future__ import annotations
import hmac
import os
from datetime import timedelta
from decimal import Decimal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .app import (
    Account, Actor, ApprovalRequest, AuditLog, Binding, Checkpoint, Closure, Contract, Entry,
    FundingRequest, Membership, Organization, Panel, PanelOwner, SystemSetting, Transaction,
    account, assert_depth_allowed, audit, balance, configured_root_id, db,
    effective_role, membership_for, now, put_setting, transfer, validate_panel_url, web_identity,
)
from .rbac import can, denial_fa, permission_payload, role_fa
from .outbox import enqueue
from .pasarguard import Client
from .secrets import encrypt_secret, resolve_secret

router = APIRouter()

APPROVAL_TTL_SECONDS = int(os.getenv("APPROVAL_TTL_SECONDS", "604800"))
ADJUST = "wallet.adjust"


class BootstrapIn(BaseModel):
    business_name: str = Field(min_length=2, max_length=160)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$")
    setup_token: str = Field(min_length=32, max_length=256)

class WebPanelIn(BaseModel):
    # Only secret *material* is accepted, and only to be encrypted here; a caller may not
    # choose how it is stored by submitting an already-resolved reference.
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=120)
    base_url: str = Field(min_length=10, max_length=500)
    api_key: str = Field(min_length=8, max_length=500)
    owner_username: str | None = Field(default=None, max_length=200)
    owner_password: str | None = Field(default=None, max_length=500)
    pg_admin_id: int | None = Field(default=None, gt=0)
    admin_username: str | None = Field(default=None, max_length=80)
    usage_coefficient: Decimal = Field(default=Decimal("1"), gt=Decimal("0"), le=Decimal("1000"))
    verify_tls: bool = True


class AdminLimitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit_use_in_bytes: int = Field(ge=0)


class CoefficientIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    usage_coefficient: Decimal = Field(gt=Decimal("0"), le=Decimal("1000"))


class AdminResellerIn(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$")
    telegram_id: int = Field(gt=0)
    price_per_gib_irr: int = Field(gt=0)
    credit_limit_irr: int = Field(default=0, ge=0)


class AdjustmentIn(BaseModel):
    organization_id: str
    amount_irr: int = Field(ne=0)
    note: str = Field(min_length=10, max_length=2000)


class FundingRequestIn(BaseModel): amount_irr: int = Field(gt=0, le=10_000_000_000_000)


class WebChildIn(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$")
    price_per_gib_irr: int = Field(gt=0)
    credit_limit_irr: int = Field(default=0, ge=0)


def require_root(identity):
    root = configured_root_id()
    if root is None:
        raise HTTPException(503, "Telegram administrator is not configured")
    if identity.user_id != root:
        raise HTTPException(403, "system administrator permission required")


def optional_membership(s: Session, telegram_id: int):
    actor = s.scalar(select(Actor).where(Actor.telegram_id == telegram_id, Actor.status == "active"))
    if not actor:
        return None
    membership = s.scalar(select(Membership).where(Membership.actor_id == actor.id, Membership.status == "active"))
    return (actor, membership, s.get(Organization, membership.organization_id)) if membership else None


def manager(s: Session, telegram_id: int, permission: str):
    """Resolve the caller's effective role and refuse the route without that permission."""
    actor, membership, organization = membership_for(s, telegram_id)
    if not can(effective_role(telegram_id, membership.role), permission):
        raise HTTPException(403, denial_fa(permission))
    return actor, membership, organization


def owned_panel(s: Session, panel_id: str, organization_id: str):
    link = s.scalar(select(PanelOwner).where(PanelOwner.panel_id == panel_id, PanelOwner.organization_id == organization_id))
    panel = s.get(Panel, panel_id) if link else None
    if not panel or panel.status != "active":
        raise HTTPException(404, "server not found")
    return panel


def panel_client(panel: Panel):
    return Client(panel.base_url, resolve_secret(panel.api_key_ref), resolve_secret(panel.owner_user_ref), resolve_secret(panel.owner_pass_ref), panel.verify_tls)


def org_binding(s: Session, organization_id: str, panel_id: str) -> Binding:
    binding = s.scalar(select(Binding).where(Binding.organization_id == organization_id, Binding.panel_id == panel_id))
    if not binding:
        raise HTTPException(409, "this organization has no billing binding on that server")
    return binding


def _enabled_flag(value: str) -> bool:
    if value == "enable":
        return True
    if value == "disable":
        return False
    raise HTTPException(422, "operation must be enable or disable")


def _unwrap(result, key: str):
    if isinstance(result, list):
        return result
    rows = (result or {}).get(key, result if isinstance(result, dict) else [])
    return rows if isinstance(rows, list) else []


def panel_operation(s: Session, panel: Panel, organization_id: str, action: str, run, actor_id: str | None = None, metadata: dict | None = None):
    """Run one panel call for an owned server, auditing it and translating failures safely.

    The upstream exception text can contain the request URL, so only a fixed
    description and the status code ever reach the caller.
    """
    try:
        result = run(panel_client(panel))
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        audit(s, f"{action}.failed", "panel", panel.id, actor_id=actor_id, organization_id=organization_id,
              metadata={"status_code": code, **(metadata or {})})
        s.commit()
        if code in (401, 403):
            raise HTTPException(502, "اعتبارنامهٔ ذخیره‌شده در PasarGuard پذیرفته نشد") from exc
        raise HTTPException(502, f"سرور PasarGuard این عملیات را رد کرد (HTTP {code})") from exc
    except Exception as exc:
        audit(s, f"{action}.failed", "panel", panel.id, actor_id=actor_id, organization_id=organization_id,
              metadata={"unreachable": True, **(metadata or {})})
        s.commit()
        raise HTTPException(502, "اتصال به PasarGuard برقرار نشد") from exc
    audit(s, action, "panel", panel.id, actor_id=actor_id, organization_id=organization_id, metadata=metadata or {})
    s.commit()
    return result


# --------------------------------------------------------------------------------------
# Session, workspace bootstrap and capabilities
# --------------------------------------------------------------------------------------

@router.get("/v1/webapp/session")
def web_session(identity=Depends(web_identity), s: Session = Depends(db)):
    linked = optional_membership(s, identity.user_id)
    if not linked:
        return {"needs_setup": True, "telegram_id": identity.user_id}
    actor, membership, organization = linked
    role = effective_role(identity.user_id, membership.role)
    return {"needs_setup": False, "actor": {"name": actor.display_name, "role": role, "role_fa": role_fa(role)},
            "organization": {"id": organization.id, "name": organization.name}}


@router.post("/v1/onboarding/bootstrap")
def bootstrap_workspace(x: BootstrapIn, identity=Depends(web_identity), s: Session = Depends(db)):
    expected = os.getenv("INITIAL_SETUP_TOKEN", "")
    if len(expected) < 32 or not hmac.compare_digest(expected, x.setup_token):
        raise HTTPException(401, "invalid setup token")
    if s.get(SystemSetting, "bootstrap_complete"):
        raise HTTPException(409, "workspace setup is already complete")
    if s.scalar(select(Organization.id).where(Organization.slug == x.slug)):
        raise HTTPException(409, "slug exists")
    organization = Organization(name=x.business_name, slug=x.slug)
    s.add(organization); s.flush()
    s.add(Closure(ancestor_id=organization.id, descendant_id=organization.id, depth=0)); account(s, organization.id)
    actor = s.scalar(select(Actor).where(Actor.telegram_id == identity.user_id))
    if not actor:
        actor = Actor(telegram_id=identity.user_id, display_name=identity.first_name)
        s.add(actor); s.flush()
    s.add(Membership(organization_id=organization.id, actor_id=actor.id, role="reseller_admin"))
    put_setting(s, "bootstrap_complete", organization.id)
    audit(s, "workspace.bootstrap", "organization", organization.id, actor_id=actor.id, organization_id=organization.id, metadata={"slug": organization.slug})
    s.commit()
    return {"organization_id": organization.id, "actor_id": actor.id}


@router.get("/v1/webapp/capabilities")
def web_capabilities(identity=Depends(web_identity), s: Session = Depends(db)):
    _, membership, organization = membership_for(s, identity.user_id)
    role = effective_role(identity.user_id, membership.role)
    return {"role": role, "role_fa": role_fa(role), "is_system_admin": role == "system_admin",
            "organization_id": organization.id, "permissions": permission_payload(role)}


@router.get("/v1/webapp/dashboard")
def web_dashboard(identity=Depends(web_identity), s: Session = Depends(db)):
    actor, membership, organization = membership_for(s, identity.user_id)
    role = effective_role(identity.user_id, membership.role)
    wallet = balance(s, organization.id)
    children = int(s.scalar(select(func.count()).select_from(Organization).where(Organization.parent_id == organization.id)) or 0)
    binding = s.scalar(select(Binding).where(Binding.organization_id == organization.id))
    cp = s.get(Checkpoint, binding.id) if binding else None
    return {"actor": {"name": actor.display_name, "role": role, "role_fa": role_fa(role)},
            "organization": {"id": organization.id, "name": organization.name, "status": organization.status},
            "wallet": {"balance_irr": wallet, "credit_limit_irr": organization.credit_limit_irr,
                       "available_irr": wallet + organization.credit_limit_irr},
            "usage": {"lifetime_bytes": cp.lifetime_bytes if cp else 0, "observed_at": cp.observed_at if cp else None},
            "children_count": children, "permissions": permission_payload(role)}


@router.get("/v1/webapp/children")
def web_children(identity=Depends(web_identity), s: Session = Depends(db)):
    _, _, organization = membership_for(s, identity.user_id)
    rows = s.scalars(select(Organization).where(Organization.parent_id == organization.id)
                     .order_by(Organization.created_at.desc()).limit(100)).all()
    return [{"id": x.id, "name": x.name, "status": x.status, "balance_irr": balance(s, x.id),
             "credit_limit_irr": x.credit_limit_irr} for x in rows]


@router.post("/v1/webapp/children")
def web_create_child(x: WebChildIn, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, parent = manager(s, identity.user_id, "manage_resellers")
    if s.scalar(select(Organization.id).where(Organization.slug == x.slug)):
        raise HTTPException(409, "slug exists")
    assert_depth_allowed(s, parent)
    child = Organization(name=x.name, slug=x.slug, parent_id=parent.id, credit_limit_irr=x.credit_limit_irr)
    s.add(child); s.flush(); s.add(Closure(ancestor_id=child.id, descendant_id=child.id, depth=0))
    for edge in s.scalars(select(Closure).where(Closure.descendant_id == parent.id)).all():
        s.add(Closure(ancestor_id=edge.ancestor_id, descendant_id=child.id, depth=edge.depth + 1))
    s.add(Contract(parent_id=parent.id, child_id=child.id, price_per_gib_irr=x.price_per_gib_irr)); account(s, child.id)
    audit(s, "organization.create", "organization", child.id, actor_id=actor.id, organization_id=parent.id,
          metadata={"slug": child.slug, "price_per_gib_irr": x.price_per_gib_irr})
    s.commit()
    return {"id": child.id, "name": child.name}


@router.get("/v1/webapp/transactions")
def web_transactions(identity=Depends(web_identity), s: Session = Depends(db)):
    _, _, organization = membership_for(s, identity.user_id)
    wallet_account = account(s, organization.id)
    rows = s.execute(select(Transaction, Entry).join(Entry, Entry.transaction_id == Transaction.id)
                     .where(Entry.account_id == wallet_account.id)
                     .order_by(Transaction.created_at.desc()).limit(50)).all()
    return [{"id": tx.id, "kind": tx.kind, "amount_irr": entry.amount_irr, "side": entry.side, "created_at": tx.created_at}
            for tx, entry in rows]


# --------------------------------------------------------------------------------------
# PasarGuard servers, nodes and subscribers
# --------------------------------------------------------------------------------------

@router.get("/v1/webapp/panels")
def web_panels(identity=Depends(web_identity), s: Session = Depends(db)):
    _, _, organization = manager(s, identity.user_id, "manage_servers")
    rows = s.scalars(select(Panel).join(PanelOwner, PanelOwner.panel_id == Panel.id)
                     .where(PanelOwner.organization_id == organization.id).order_by(Panel.name)).all()
    return [{"id": p.id, "name": p.name, "base_url": p.base_url, "status": p.status} for p in rows]


@router.post("/v1/webapp/panels")
def web_create_panel(x: WebPanelIn, identity=Depends(web_identity), s: Session = Depends(db)):
    _, membership, organization = manager(s, identity.user_id, "manage_servers")
    validate_panel_url(x.base_url)
    verify_tls = x.verify_tls
    # Trusting an unverified certificate is a workspace-wide risk, so it stays a root decision.
    if not verify_tls and effective_role(identity.user_id, membership.role) != "system_admin":
        raise HTTPException(403, "تنها مدیر کل سیستم می‌تواند اعتبارسنجی گواهی TLS را غیرفعال کند")
    url = x.base_url.rstrip("/")
    try:
        probe = Client(url, x.api_key, x.owner_username, x.owner_password, verify_tls).nodes()
    except Exception as exc:
        # The upstream failure can echo the request URL; never let that reach the client log.
        raise HTTPException(422, "PasarGuard connection failed: the server did not answer the test request") from exc
    panel = Panel(name=x.name, base_url=url, api_key_ref=encrypt_secret(x.api_key),
                  owner_user_ref=encrypt_secret(x.owner_username), owner_pass_ref=encrypt_secret(x.owner_password),
                  usage_coefficient=x.usage_coefficient, verify_tls=verify_tls)
    s.add(panel); s.flush(); s.add(PanelOwner(panel_id=panel.id, organization_id=organization.id))
    if x.pg_admin_id:
        if s.scalar(select(Binding.id).where(Binding.organization_id == organization.id)):
            raise HTTPException(409, "this organization already has a billing binding")
        s.add(Binding(organization_id=organization.id, panel_id=panel.id, pg_admin_id=x.pg_admin_id,
                      username=x.admin_username or str(x.pg_admin_id)))
    audit(s, "panel.create", "panel", panel.id, organization_id=organization.id,
          metadata={"base_url": panel.base_url, "usage_coefficient": str(panel.usage_coefficient),
                    "verify_tls": verify_tls})
    s.commit()
    nodes = probe.get("nodes", probe if isinstance(probe, list) else [])
    return {"id": panel.id, "name": panel.name, "status": panel.status, "nodes_detected": len(nodes) if isinstance(nodes, list) else 0}


@router.get("/v1/webapp/panels/{panel_id}/nodes")
def web_panel_nodes(panel_id: str, identity=Depends(web_identity), s: Session = Depends(db)):
    _, _, organization = manager(s, identity.user_id, "manage_servers"); panel = owned_panel(s, panel_id, organization.id)
    nodes = panel_operation(s, panel, organization.id, "panel.nodes.read", lambda c: c.nodes())
    return {"panel_id": panel.id, "nodes": _unwrap(nodes, "nodes")}


@router.post("/v1/webapp/panels/{panel_id}/nodes/{node_id}/reconnect")
def web_reconnect_node(panel_id: str, node_id: int, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id, "node_control"); panel = owned_panel(s, panel_id, organization.id)
    result = panel_operation(s, panel, organization.id, "node.reconnect", lambda c: c.reconnect_node(node_id), actor.id, {"node_id": node_id})
    return {"ok": True, "result": result}


@router.get("/v1/webapp/panels/{panel_id}/nodes/{node_id}/status")
def web_node_status(panel_id: str, node_id: int, identity=Depends(web_identity), s: Session = Depends(db)):
    _, _, organization = manager(s, identity.user_id, "node_control"); panel = owned_panel(s, panel_id, organization.id)
    return panel_operation(s, panel, organization.id, "node.status.read", lambda c: c.node_status(node_id))


@router.post("/v1/webapp/panels/{panel_id}/nodes/{node_id}/reset")
def web_reset_node(panel_id: str, node_id: int, identity=Depends(web_identity), s: Session = Depends(db)):
    """Declared before the enable/disable route: ``reset`` must not be read as an ``enabled`` value."""
    actor, _, organization = manager(s, identity.user_id, "node_control"); panel = owned_panel(s, panel_id, organization.id)
    result = panel_operation(s, panel, organization.id, "node.reset", lambda c: c.reset_node(node_id), actor.id, {"node_id": node_id})
    return {"ok": True, "result": result}


@router.post("/v1/webapp/panels/{panel_id}/nodes/{node_id}/{enabled}")
def web_set_node_enabled(panel_id: str, node_id: int, enabled: str, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id, "node_control"); panel = owned_panel(s, panel_id, organization.id)
    flag = _enabled_flag(enabled)
    result = panel_operation(s, panel, organization.id, f"node.{'enable' if flag else 'disable'}",
                             lambda c: c.set_node_enabled(node_id, flag), actor.id, {"node_id": node_id})
    return {"ok": True, "enabled": flag, "result": result}


@router.get("/v1/webapp/panels/{panel_id}/users")
def web_panel_users(panel_id: str, offset: int = 0, limit: int = 50, identity=Depends(web_identity), s: Session = Depends(db)):
    _, _, organization = manager(s, identity.user_id, "subscriber_control"); panel = owned_panel(s, panel_id, organization.id)
    binding = org_binding(s, organization.id, panel.id)
    if limit < 1 or limit > 200:
        raise HTTPException(422, "limit must be between 1 and 200")
    users = panel_operation(s, panel, organization.id, "user.list", lambda c: c.users(binding.pg_admin_id, offset, limit))
    return {"panel_id": panel.id, "admin_id": binding.pg_admin_id, "offset": offset, "limit": limit,
            "users": _unwrap(users, "users"), "total": users.get("total") if isinstance(users, dict) else None}


@router.post("/v1/webapp/panels/{panel_id}/users/{user_id}/reset-data")
def web_reset_user_data(panel_id: str, user_id: int, identity=Depends(web_identity), s: Session = Depends(db)):
    """Clears a subscriber's counters on the panel; destructive, so it is always audited."""
    actor, _, organization = manager(s, identity.user_id, "subscriber_control"); panel = owned_panel(s, panel_id, organization.id)
    result = panel_operation(s, panel, organization.id, "user.reset_data", lambda c: c.reset_user_data(user_id), actor.id, {"user_id": user_id})
    return {"ok": True, "result": result}


@router.post("/v1/webapp/panels/{panel_id}/users/{user_id}/{enabled}")
def web_set_user_enabled(panel_id: str, user_id: int, enabled: str, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id, "subscriber_control"); panel = owned_panel(s, panel_id, organization.id)
    flag = _enabled_flag(enabled)
    result = panel_operation(s, panel, organization.id, f"user.{'enable' if flag else 'disable'}",
                             lambda c: c.set_user_enabled(user_id, flag), actor.id, {"user_id": user_id})
    return {"ok": True, "enabled": flag, "result": result}


@router.post("/v1/webapp/panels/{panel_id}/limit")
def web_set_admin_limit(panel_id: str, x: AdminLimitIn, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id, "admin_limit"); panel = owned_panel(s, panel_id, organization.id)
    binding = org_binding(s, organization.id, panel.id)
    result = panel_operation(s, panel, organization.id, "admin.limit",
                             lambda c: c.set_admin_limit(binding.pg_admin_id, x.limit_use_in_bytes), actor.id,
                             {"pg_admin_id": binding.pg_admin_id, "limit_use_in_bytes": x.limit_use_in_bytes})
    return {"ok": True, "result": result}


@router.post("/v1/webapp/panels/{panel_id}/coefficient")
def web_set_usage_coefficient(panel_id: str, x: CoefficientIn, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id, "change_billing_coefficient")
    panel = owned_panel(s, panel_id, organization.id)
    panel.usage_coefficient = x.usage_coefficient
    audit(s, "panel.coefficient", "panel", panel.id, actor_id=actor.id, organization_id=organization.id,
          metadata={"usage_coefficient": str(x.usage_coefficient)})
    s.commit()
    s.refresh(panel)
    return {"id": panel.id, "usage_coefficient": str(panel.usage_coefficient)}


# --------------------------------------------------------------------------------------
# Funding, reseller administration and approvals
# --------------------------------------------------------------------------------------

@router.post("/v1/webapp/funding-requests")
def web_funding_request(x: FundingRequestIn, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id, "request_funding")
    request = FundingRequest(organization_id=organization.id, actor_id=actor.id, amount_irr=x.amount_irr)
    s.add(request)
    audit(s, "funding_request.create", "funding_request", request.id, actor_id=actor.id,
          organization_id=organization.id, metadata={"amount_irr": x.amount_irr})
    s.commit()
    return {"id": request.id, "status": request.status}


def pending_funding_rows(s: Session, branch_id: str, include_rootless: bool = False):
    """Requests waiting for the money of this branch: the caller pays, so the caller decides."""
    owning = Organization.parent_id == branch_id
    if include_rootless:
        # A top level organization has no parent, so only the root workspace can fund it.
        owning = owning | Organization.parent_id.is_(None)
    rows = s.execute(select(FundingRequest, Organization, Actor)
                     .join(Organization, Organization.id == FundingRequest.organization_id)
                     .join(Actor, Actor.id == FundingRequest.actor_id)
                     .where(FundingRequest.status == "pending", owning)
                     .order_by(FundingRequest.created_at.asc()).limit(100)).all()
    return [{"id": request.id, "organization": organization.name, "organization_id": organization.id,
             "telegram_id": actor.telegram_id, "amount_irr": request.amount_irr, "status": request.status,
             "created_at": request.created_at} for request, organization, actor in rows]


def decide_funding_request(s: Session, actor_id: str, branch_id: str, request_id: str, decision: str,
                           allow_rootless: bool = False):
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
    # Only the direct parent releases the credit, so a reseller can never fund another branch.
    if child.parent_id != branch_id and not (allow_rootless and child.parent_id is None):
        raise HTTPException(403, "this request belongs to another reseller branch")
    if decision == "approve":
        transfer(s, "SYSTEM", request.organization_id, request.amount_irr, f"funding-request:{request.id}",
                 "fund", request.id, False, actor_id)
        request.status = "approved"
    else:
        request.status = "rejected"
    request.decided_by = actor_id
    request.decided_at = now()
    audit(s, f"funding_request.{decision}", "funding_request", request.id, actor_id=actor_id,
          organization_id=request.organization_id, metadata={"amount_irr": request.amount_irr, "decided_by": actor_id})
    s.commit()
    return {"id": request.id, "status": request.status}


@router.get("/v1/webapp/funding-requests")
def branch_funding_requests(identity=Depends(web_identity), s: Session = Depends(db)):
    """Pending requests of the caller's own direct resellers."""
    _, _, organization = manager(s, identity.user_id, "decide_funding")
    return pending_funding_rows(s, organization.id)


@router.post("/v1/webapp/funding-requests/{request_id}/{decision}")
def branch_decide_funding(request_id: str, decision: str, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id, "decide_funding")
    return decide_funding_request(s, actor.id, organization.id, request_id, decision)


@router.get("/v1/webapp/admin/overview")
def admin_overview(identity=Depends(web_identity), s: Session = Depends(db)):
    require_root(identity); membership_for(s, identity.user_id)
    return {"organizations": int(s.scalar(select(func.count()).select_from(Organization)) or 0),
            "active_organizations": int(s.scalar(select(func.count()).select_from(Organization).where(Organization.status == "active")) or 0),
            "panels": int(s.scalar(select(func.count()).select_from(Panel).where(Panel.status == "active")) or 0),
            "actors": int(s.scalar(select(func.count()).select_from(Actor).where(Actor.status == "active")) or 0),
            "pending_funding": int(s.scalar(select(func.count()).select_from(FundingRequest).where(FundingRequest.status == "pending")) or 0),
            "bindings": int(s.scalar(select(func.count()).select_from(Binding).where(Binding.status == "active")) or 0)}


@router.get("/v1/webapp/admin/resellers")
def admin_resellers(identity=Depends(web_identity), s: Session = Depends(db)):
    require_root(identity); _, _, root_org = membership_for(s, identity.user_id)
    rows = s.scalars(select(Organization).where(Organization.parent_id == root_org.id)
                     .order_by(Organization.created_at.desc()).limit(200)).all()
    result = []
    for org in rows:
        wallet = s.scalar(select(Account).where(Account.owner_key == org.id, Account.code == "wallet"))
        actor = s.scalar(select(Actor).join(Membership, Membership.actor_id == Actor.id)
                         .where(Membership.organization_id == org.id, Membership.status == "active"))
        contract = s.scalar(select(Contract).where(Contract.child_id == org.id, Contract.status == "active"))
        result.append({"id": org.id, "name": org.name, "slug": org.slug, "status": org.status,
                       "telegram_id": actor.telegram_id if actor else None,
                       "balance_irr": int(wallet.balance_irr if wallet else 0),
                       "credit_limit_irr": int(org.credit_limit_irr),
                       "price_per_gib_irr": int(contract.price_per_gib_irr if contract else 0),
                       "created_at": org.created_at})
    return result


@router.post("/v1/webapp/admin/resellers")
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


@router.post("/v1/webapp/admin/resellers/{organization_id}/status")
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


@router.get("/v1/webapp/admin/funding-requests")
def admin_funding_requests(identity=Depends(web_identity), s: Session = Depends(db)):
    require_root(identity); _, _, root_org = membership_for(s, identity.user_id)
    return pending_funding_rows(s, root_org.id, include_rootless=True)


@router.post("/v1/webapp/admin/funding-requests/{request_id}/{decision}")
def admin_decide_funding(request_id: str, decision: str, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, root_org = membership_for(s, identity.user_id)
    require_root(identity)
    return decide_funding_request(s, actor.id, root_org.id, request_id, decision, allow_rootless=True)


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


@router.post("/v1/webapp/admin/adjustments")
def request_adjustment(x: AdjustmentIn, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, membership, organization = membership_for(s, identity.user_id)
    role = effective_role(identity.user_id, membership.role)
    if not can(role, "request_adjustment"):
        raise HTTPException(403, denial_fa("request_adjustment"))
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


@router.get("/v1/webapp/admin/approvals")
def list_approvals(identity=Depends(web_identity), s: Session = Depends(db)):
    require_root(identity); membership_for(s, identity.user_id)
    rows = s.scalars(select(ApprovalRequest).where(ApprovalRequest.status == "pending")
                     .order_by(ApprovalRequest.created_at).limit(200)).all()
    return [approval_payload(a) for a in rows]


@router.post("/v1/webapp/admin/approvals/{approval_id}/{decision}")
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
    enqueue(s, "approval.granted", approval.id, {"action": approval.action, "status": approval.status})
    s.commit()
    return {"id": approval.id, "status": approval.status}


@router.get("/v1/webapp/audit")
def web_audit(limit: int = 50, identity=Depends(web_identity), s: Session = Depends(db)):
    """Audit rows of the caller's own subtree; the root administrator sees the whole workspace."""
    actor, _, organization = manager(s, identity.user_id, "view_audit")
    if limit < 1 or limit > 200:
        raise HTTPException(422, "limit must be between 1 and 200")
    subtree = select(Closure.descendant_id).where(Closure.ancestor_id == organization.id)
    conditions = []
    if identity.user_id != configured_root_id():
        conditions.append(AuditLog.organization_id.in_(subtree))
    rows = s.execute(select(AuditLog, Actor, Organization)
                     .outerjoin(Actor, Actor.id == AuditLog.actor_id)
                     .outerjoin(Organization, Organization.id == AuditLog.organization_id)
                     .where(*conditions).order_by(AuditLog.created_at.desc()).limit(limit)).all()
    return [{"id": a.id, "action": a.action, "target_type": a.target_type, "target_id": a.target_id,
             "organization_id": a.organization_id, "organization": organization.name if organization else None,
             "actor": actor.display_name if actor else None, "metadata": a.details, "created_at": a.created_at}
            for a, actor, organization in rows]
