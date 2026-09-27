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
from secrets import token_hex

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .app import (
    Account, Actor, ApprovalRequest, AuditLog, Binding, Checkpoint, Closure, Contract, Entry,
    FundingRequest, Membership, ORDER_STATES, Organization, Panel, PanelOrder, PanelOwner, Plan, PlanCategory,
    SystemSetting, Transaction,
    account, assert_depth_allowed, assert_vendor, audit, balance, configured_root_id, db,
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


def accessible_panel(s: Session, panel_id: str, organization_id: str):
    """A server this organization owns, or the one an approved order bound it to.

    Ownership of the mother panel stays with the seller after an order is approved, so subscriber
    administration is opened to the buyer through its binding and nothing else.
    """
    owned = s.scalar(select(PanelOwner.panel_id).where(PanelOwner.panel_id == panel_id,
                                                       PanelOwner.organization_id == organization_id))
    bound = s.scalar(select(Binding.id).where(Binding.panel_id == panel_id,
                                              Binding.organization_id == organization_id))
    panel = s.get(Panel, panel_id) if owned or bound else None
    if not panel or panel.status != "active":
        raise HTTPException(404, "server not found")
    return panel


def panel_client(panel: Panel):
    assert_vendor(panel)
    return Client(panel.base_url, resolve_secret(panel.api_key_ref), resolve_secret(panel.owner_user_ref), resolve_secret(panel.owner_pass_ref), panel.verify_tls)


def org_binding(s: Session, organization_id: str, panel_id: str) -> Binding:
    binding = s.scalar(select(Binding).where(Binding.organization_id == organization_id, Binding.panel_id == panel_id))
    if not binding:
        raise HTTPException(409, "this organization has no billing binding on that server")
    return binding


def panel_is_mine(s: Session, panel_id: str, organization_id: str) -> bool:
    return bool(s.scalar(select(PanelOwner.panel_id).where(PanelOwner.panel_id == panel_id,
                                                           PanelOwner.organization_id == organization_id)))


def assert_bound_subscriber(s: Session, panel: Panel, organization_id: str, binding: Binding,
                            user_id: int, actor_id: str | None) -> None:
    """Refuse a subscriber the panel attributes to another admin before a buyer touches it.

    Panel user ids are sequential, so the list a customer sees is not a boundary; the owning admin
    reported by the panel itself is. Sellers that own the server keep operating every subscriber.
    """
    row = panel_operation(s, panel, organization_id, "user.read", lambda c: c.user(user_id), actor_id,
                          {"user_id": user_id})
    if isinstance(row, dict):
        row = row.get("user", row)
    owner = row.get("admin_id") if isinstance(row, dict) else None
    try:
        belongs = int(owner) == binding.pg_admin_id
    except (TypeError, ValueError):
        belongs = False
    if not belongs:
        raise HTTPException(403, "این مشترک به پنل شما تعلق ندارد")


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
            "area": "customer" if role == "customer" else "staff",
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
    return [{"id": p.id, "name": p.name, "base_url": p.base_url, "status": p.status, "saleable": p.saleable} for p in rows]


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


@router.get("/v1/webapp/my-panel")
def web_my_panel(identity=Depends(web_identity), s: Session = Depends(db)):
    """The server behind the caller's own account: what an approved order bound to this organization.

    Deliberately narrow — the mother panel stays the seller's infrastructure, so neither its
    address nor any credential appears here.
    """
    _, _, organization = manager(s, identity.user_id, "subscriber_control")
    binding = s.scalar(select(Binding).where(Binding.organization_id == organization.id))
    if not binding:
        raise HTTPException(404, "هنوز پنلی به این حساب متصل نشده است")
    panel = s.get(Panel, binding.panel_id)
    checkpoint = s.get(Checkpoint, binding.id)
    return {"panel_id": panel.id, "name": panel.name, "status": panel.status, "admin_username": binding.username,
            "lifetime_bytes": checkpoint.lifetime_bytes if checkpoint else 0,
            "observed_at": checkpoint.observed_at if checkpoint else None}


@router.get("/v1/webapp/panels/{panel_id}/users")
def web_panel_users(panel_id: str, offset: int = 0, limit: int = 50, identity=Depends(web_identity), s: Session = Depends(db)):
    _, _, organization = manager(s, identity.user_id, "subscriber_control"); panel = accessible_panel(s, panel_id, organization.id)
    binding = org_binding(s, organization.id, panel.id)
    if limit < 1 or limit > 200:
        raise HTTPException(422, "limit must be between 1 and 200")
    users = panel_operation(s, panel, organization.id, "user.list", lambda c: c.users(binding.pg_admin_id, offset, limit))
    return {"panel_id": panel.id, "admin_id": binding.pg_admin_id, "offset": offset, "limit": limit,
            "users": _unwrap(users, "users"), "total": users.get("total") if isinstance(users, dict) else None}


@router.post("/v1/webapp/panels/{panel_id}/users/{user_id}/reset-data")
def web_reset_user_data(panel_id: str, user_id: int, identity=Depends(web_identity), s: Session = Depends(db)):
    """Clears a subscriber's counters on the panel; destructive, so it is always audited."""
    actor, _, organization = manager(s, identity.user_id, "subscriber_control"); panel = accessible_panel(s, panel_id, organization.id)
    if not panel_is_mine(s, panel.id, organization.id):
        assert_bound_subscriber(s, panel, organization.id, org_binding(s, organization.id, panel.id), user_id, actor.id)
    result = panel_operation(s, panel, organization.id, "user.reset_data", lambda c: c.reset_user_data(user_id), actor.id, {"user_id": user_id})
    return {"ok": True, "result": result}


@router.post("/v1/webapp/panels/{panel_id}/users/{user_id}/{enabled}")
def web_set_user_enabled(panel_id: str, user_id: int, enabled: str, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id, "subscriber_control"); panel = accessible_panel(s, panel_id, organization.id)
    flag = _enabled_flag(enabled)
    if not panel_is_mine(s, panel.id, organization.id):
        assert_bound_subscriber(s, panel, organization.id, org_binding(s, organization.id, panel.id), user_id, actor.id)
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


# --------------------------------------------------------------------------------------
# The customer funnel: order a panel, get quoted, pay, get an account
# --------------------------------------------------------------------------------------

class OrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    panel_id: str
    # A published plan prices the order itself; leaving it empty asks the seller to quote by hand.
    plan_id: str | None = None
    business_name: str = Field(min_length=2, max_length=160)
    user_count: int = Field(gt=0, le=100_000)
    daily_gib: int = Field(gt=0, le=100_000)
    note: str = Field(default="", max_length=500)


class OrderQuoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_irr: int = Field(gt=0, le=10_000_000_000_000)
    credit_limit_irr: int = Field(default=0, ge=0)
    price_per_gib_irr: int = Field(gt=0)
    payment_instructions: str = Field(min_length=3, max_length=2000)
    # The login id of a panel admin the reviewer has already created on the mother server. Binding
    # is optional because the control plane does not create panel accounts on its own.
    pg_admin_id: int | None = Field(default=None, gt=0)


class OrderRejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="", max_length=500)


class OrderApproveIn(BaseModel):
    """A plan order never passes through the quote form, so the panel account can arrive here."""
    model_config = ConfigDict(extra="forbid")
    pg_admin_id: int | None = Field(default=None, gt=0)
    admin_username: str | None = Field(default=None, max_length=80)


ORDER_FLOW: dict[str, set[str]] = {
    "pending": {"quoted", "rejected", "cancelled"},
    "quoted": {"confirmed", "rejected", "cancelled"},
    "confirmed": {"payment_declared", "rejected", "cancelled"},
    "payment_declared": {"approved", "rejected"},
    "approved": set(), "rejected": set(), "cancelled": set(),
}


def order_permission(s: Session, identity, permission: str) -> None:
    """A linked account obeys the matrix; an unlinked one may only knock on the door."""
    linked = optional_membership(s, identity.user_id)
    if not linked:
        return
    _, membership, _ = linked
    if not can(effective_role(identity.user_id, membership.role), permission):
        raise HTTPException(403, denial_fa(permission))


def requester_actor(s: Session, telegram_id: int, display_name: str = "", *, create: bool = False) -> Actor | None:
    actor = s.scalar(select(Actor).where(Actor.telegram_id == telegram_id))
    if not actor and create:
        actor = Actor(telegram_id=telegram_id, display_name=display_name)
        s.add(actor)
        s.flush()
    return actor


def unique_order_slug(s: Session) -> str:
    # The organization inherits this slug on approval, so both tables have to be free.
    for _ in range(20):
        candidate = f"panel-{token_hex(4)}"
        if not s.scalar(select(PanelOrder.id).where(PanelOrder.slug == candidate)) \
                and not s.scalar(select(Organization.id).where(Organization.slug == candidate)):
            return candidate
    raise HTTPException(500, "unable to allocate an order slug")


def order_row(s: Session, order: PanelOrder) -> dict:
    panel = s.get(Panel, order.panel_id)
    plan = s.get(Plan, order.plan_id) if order.plan_id else None
    return {"id": order.id, "status": order.status, "business_name": order.business_name, "slug": order.slug,
            "panel_id": order.panel_id, "panel_name": panel.name if panel else None,
            "plan_id": order.plan_id, "plan_name": plan.name if plan else None,
            "duration_days": plan.duration_days if plan else None,
            "pg_admin_id": order.pg_admin_id, "admin_username": order.admin_username,
            "user_count": order.user_count, "daily_gib": order.daily_gib, "note": order.note,
            "amount_irr": order.amount_irr, "credit_limit_irr": order.credit_limit_irr,
            "price_per_gib_irr": order.price_per_gib_irr, "payment_instructions": order.payment_instructions,
            "organization_id": order.organization_id, "rejection_reason": order.rejection_reason,
            "created_at": order.created_at, "decided_at": order.decided_at}


def require_transition(order: PanelOrder, to_status: str) -> None:
    if to_status not in ORDER_FLOW.get(order.status, set()):
        raise HTTPException(409, f"سفارش در وضعیت «{order.status}» قابل «{to_status}» نیست")


def locked_order(s: Session, order_id: str, to_status: str) -> PanelOrder:
    order = s.scalar(select(PanelOrder).where(PanelOrder.id == order_id).with_for_update())
    if not order:
        raise HTTPException(404, "order not found")
    require_transition(order, to_status)
    return order


def panel_saleable_rows(s: Session):
    rows = s.scalars(select(Panel).where(Panel.status == "active", Panel.saleable.is_(True)).order_by(Panel.name)).all()
    return [{"id": p.id, "name": p.name} for p in rows]


def plan_row(plan: Plan, panel_name: str | None = None) -> dict:
    return {"id": plan.id, "name": plan.name, "description": plan.description, "panel_id": plan.panel_id,
            "panel_name": panel_name, "category_id": plan.category_id, "price_per_gib_irr": plan.price_per_gib_irr,
            "daily_gib": plan.daily_gib, "duration_days": plan.duration_days, "user_count": plan.user_count,
            "credit_limit_irr": plan.credit_limit_irr, "payment_instructions": plan.payment_instructions,
            "amount_irr": plan.amount_irr, "status": plan.status}


def saleable_plan_rows(s: Session):
    """What a stranger may buy now: a published plan, on an open shelf, on a server that is on sale."""
    panels = {p.id: p.name for p in s.scalars(select(Panel).where(Panel.status == "active",
                                                                  Panel.saleable.is_(True))).all()}
    shelves = {c.id for c in s.scalars(select(PlanCategory).where(PlanCategory.status == "active")).all()}
    rows = []
    for plan in s.scalars(select(Plan).where(Plan.status == "active").order_by(Plan.price_per_gib_irr)).all():
        if plan.panel_id not in panels or (plan.category_id and plan.category_id not in shelves):
            continue
        rows.append(plan_row(plan, panels[plan.panel_id]))
    return rows


def priced_plan(s: Session, panel: Panel, plan_id: str | None) -> Plan | None:
    if not plan_id:
        return None
    plan = s.get(Plan, plan_id)
    if not plan or plan.status != "active" or plan.panel_id != panel.id:
        raise HTTPException(422, "این پلن برای فروش روی این سرور باز نیست")
    if plan.category_id:
        shelf = s.get(PlanCategory, plan.category_id)
        if not shelf or shelf.status != "active" or shelf.organization_id != plan.organization_id:
            raise HTTPException(422, "این پلن برای فروش باز نیست")
    return plan


@router.get("/v1/webapp/order-options")
def order_options(identity=Depends(web_identity), s: Session = Depends(db)):
    """What a customer may order from: only servers the owner put on the sale list."""
    order_permission(s, identity, "order_panel")
    return {"panels": panel_saleable_rows(s), "plans": saleable_plan_rows(s)}


def create_panel_order(s: Session, *, telegram_id: int, display_name: str, via: str, panel_id: str,
                       business_name: str, user_count: int, daily_gib: int, note: str,
                       plan_id: str | None = None) -> PanelOrder:
    """Register a request for a panel. It stays a piece of chat traffic until it is priced."""
    panel = s.get(Panel, panel_id)
    if not panel or panel.status != "active" or not panel.saleable:
        raise HTTPException(422, "این سرور برای سفارش باز نیست")
    plan = priced_plan(s, panel, plan_id)
    actor = requester_actor(s, telegram_id, business_name or display_name, create=True)
    order = PanelOrder(actor_id=actor.id, panel_id=panel.id, business_name=business_name, slug=unique_order_slug(s),
                       user_count=user_count, daily_gib=daily_gib, note=note, plan_id=plan.id if plan else None)
    if plan:
        # The catalog is the only price source on this path: whatever the client sent is discarded.
        order.user_count, order.daily_gib = plan.user_count, plan.daily_gib
        order.price_per_gib_irr = plan.price_per_gib_irr
        order.amount_irr = plan.amount_irr
        order.credit_limit_irr = plan.credit_limit_irr or plan.amount_irr
        order.payment_instructions = plan.payment_instructions
        order.status = "quoted"
    s.add(order)
    audit(s, "panel_order.create", "panel_order", order.id, actor_id=actor.id,
          metadata={"panel_id": panel.id, "slug": order.slug, "user_count": order.user_count,
                    "daily_gib": order.daily_gib, "via": via, "plan_id": order.plan_id,
                    "amount_irr": order.amount_irr})
    return order


@router.post("/v1/webapp/orders")
def create_order(x: OrderIn, identity=Depends(web_identity), s: Session = Depends(db)):
    order_permission(s, identity, "order_panel")
    order = create_panel_order(s, telegram_id=identity.user_id, display_name=identity.first_name, via="webapp",
                              panel_id=x.panel_id, business_name=x.business_name, user_count=x.user_count,
                              daily_gib=x.daily_gib, note=x.note, plan_id=x.plan_id)
    s.commit()
    return order_row(s, order)


@router.get("/v1/webapp/orders")
def my_orders(identity=Depends(web_identity), s: Session = Depends(db)):
    order_permission(s, identity, "order_panel")
    actor = requester_actor(s, identity.user_id)
    if not actor:
        return []
    rows = s.scalars(select(PanelOrder).where(PanelOrder.actor_id == actor.id)
                     .order_by(PanelOrder.created_at.desc()).limit(50)).all()
    return [order_row(s, order) for order in rows]


def owned_order(s: Session, order_id: str, identity, to_status: str) -> PanelOrder:
    order = locked_order(s, order_id, to_status)
    actor = requester_actor(s, identity.user_id)
    if not actor or order.actor_id != actor.id:
        raise HTTPException(403, "این سفارش به حساب شما نیست")
    return order


CUSTOMER_TRANSITIONS = {"confirmed": "panel_order.confirm", "payment_declared": "panel_order.payment_declared",
                        "cancelled": "panel_order.cancel"}


def advance_customer_order(s: Session, order: PanelOrder, to_status: str) -> None:
    """The three steps the requester owns; the row is already locked by the caller."""
    require_transition(order, to_status)
    order.status = to_status
    audit(s, CUSTOMER_TRANSITIONS[to_status], "panel_order", order.id, actor_id=order.actor_id,
          organization_id=order.organization_id,
          metadata={"amount_irr": order.amount_irr} if to_status == "payment_declared" else None)


@router.post("/v1/webapp/orders/{order_id}/confirm")
def confirm_order(order_id: str, identity=Depends(web_identity), s: Session = Depends(db)):
    """The customer accepts the quote; payment details stay with them until they declare it."""
    order_permission(s, identity, "order_panel")
    order = owned_order(s, order_id, identity, "confirmed")
    advance_customer_order(s, order, "confirmed")
    s.commit()
    return {"id": order.id, "status": order.status}


@router.post("/v1/webapp/orders/{order_id}/payment")
def declare_payment(order_id: str, identity=Depends(web_identity), s: Session = Depends(db)):
    order_permission(s, identity, "order_panel")
    order = owned_order(s, order_id, identity, "payment_declared")
    advance_customer_order(s, order, "payment_declared")
    s.commit()
    return {"id": order.id, "status": order.status}


@router.post("/v1/webapp/orders/{order_id}/cancel")
def cancel_order(order_id: str, identity=Depends(web_identity), s: Session = Depends(db)):
    order_permission(s, identity, "order_panel")
    order = owned_order(s, order_id, identity, "cancelled")
    advance_customer_order(s, order, "cancelled")
    s.commit()
    return {"id": order.id, "status": order.status}


def review_panels(s: Session, organization_id: str):
    return select(PanelOwner.panel_id).where(PanelOwner.organization_id == organization_id)


@router.get("/v1/webapp/orders/queue")
def order_queue(status: str = "pending", identity=Depends(web_identity), s: Session = Depends(db)):
    """The reviewer only sees orders placed against the servers their own organization owns."""
    _, _, organization = manager(s, identity.user_id, "decide_orders")
    if status not in ORDER_STATES:
        raise HTTPException(422, "unknown order status")
    rows = s.scalars(select(PanelOrder).where(PanelOrder.status == status,
                                              PanelOrder.panel_id.in_(review_panels(s, organization.id)))
                      .order_by(PanelOrder.created_at).limit(100)).all()
    return [order_row(s, order) for order in rows]


def assert_order_seller(s: Session, order: PanelOrder, organization_id: str) -> None:
    if not s.scalar(select(PanelOwner).where(PanelOwner.panel_id == order.panel_id,
                                             PanelOwner.organization_id == organization_id)):
        raise HTTPException(403, "این سفارش روی سرور دیگری ثبت شده است")


def quote_panel_order(s: Session, order: PanelOrder, x: OrderQuoteIn, reviewer_id: str, organization_id: str) -> None:
    """Attach the commercial terms a customer was quoted; the row is already locked."""
    assert_order_seller(s, order, organization_id)
    require_transition(order, "quoted")
    order.credit_limit_irr = x.credit_limit_irr
    order.price_per_gib_irr = x.price_per_gib_irr
    order.amount_irr = x.amount_irr
    order.payment_instructions = x.payment_instructions
    order.admin_username = f"admin-{order.slug}" if not order.admin_username else order.admin_username
    order.pg_admin_id = x.pg_admin_id
    order.status = "quoted"
    audit(s, "panel_order.quote", "panel_order", order.id, actor_id=reviewer_id, organization_id=organization_id,
          metadata={"amount_irr": x.amount_irr, "credit_limit_irr": x.credit_limit_irr,
                    "price_per_gib_irr": x.price_per_gib_irr, "pg_admin_id": x.pg_admin_id})


@router.post("/v1/webapp/orders/{order_id}/quote")
def quote_order(order_id: str, x: OrderQuoteIn, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id, "decide_orders")
    order = locked_order(s, order_id, "quoted")
    quote_panel_order(s, order, x, actor.id, organization.id)
    s.commit()
    return order_row(s, order)


def reject_panel_order(s: Session, order: PanelOrder, reviewer_id: str, organization_id: str, reason: str) -> None:
    assert_order_seller(s, order, organization_id)
    require_transition(order, "rejected")
    order.status = "rejected"
    order.rejection_reason = reason
    order.decided_by = reviewer_id
    order.decided_at = now()
    audit(s, "panel_order.reject", "panel_order", order.id, actor_id=reviewer_id, organization_id=organization_id,
          metadata={"reason": reason})


@router.post("/v1/webapp/orders/{order_id}/reject")
def reject_order(order_id: str, x: OrderRejectIn, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id, "decide_orders")
    order = locked_order(s, order_id, "rejected")
    reject_panel_order(s, order, actor.id, organization.id, x.reason)
    s.commit()
    return {"id": order.id, "status": order.status}


def approve_panel_order(s: Session, order: PanelOrder, reviewer_id: str, seller_org_id: str,
                        attach: OrderApproveIn | None = None) -> dict:
    """Turn a paid order into a real account: organization, contract, wallet funding and access.

    The opening balance is a ledger transfer, so the offline payment is recorded the same way a
    funded reseller request is, and the ledger stays balanced by construction.
    """
    assert_order_seller(s, order, seller_org_id)
    require_transition(order, "approved")
    if attach and attach.pg_admin_id and not order.pg_admin_id:
        order.pg_admin_id = attach.pg_admin_id
        order.admin_username = (attach.admin_username or "").strip()[:80]
    panel = s.get(Panel, order.panel_id)
    parent = s.get(Organization, seller_org_id)
    assert_depth_allowed(s, parent)
    if s.scalar(select(Organization.id).where(Organization.slug == order.slug)):
        raise HTTPException(409, "slug already exists")
    child = Organization(name=order.business_name, slug=order.slug, parent_id=parent.id,
                         credit_limit_irr=order.credit_limit_irr)
    s.add(child)
    s.flush()
    s.add(Closure(ancestor_id=child.id, descendant_id=child.id, depth=0))
    for edge in s.scalars(select(Closure).where(Closure.descendant_id == parent.id)).all():
        s.add(Closure(ancestor_id=edge.ancestor_id, descendant_id=child.id, depth=edge.depth + 1))
    s.add(Contract(parent_id=parent.id, child_id=child.id, price_per_gib_irr=order.price_per_gib_irr))
    account(s, child.id)
    s.add(Membership(organization_id=child.id, actor_id=order.actor_id, role="customer", status="active"))
    # The panel itself stays owned by the seller; the customer only gains the admin account the
    # reviewer created there, recorded as this organization's binding.
    if order.pg_admin_id:
        s.add(Binding(organization_id=child.id, panel_id=panel.id, pg_admin_id=order.pg_admin_id,
                      username=order.admin_username or str(order.pg_admin_id)))
    transfer(s, "SYSTEM", child.id, order.amount_irr, f"panel-order:{order.id}", "order-funded", order.id, False,
             reviewer_id)
    order.status = "approved"
    order.organization_id = child.id
    order.decided_by = reviewer_id
    order.decided_at = now()
    audit(s, "panel_order.approve", "panel_order", order.id, actor_id=reviewer_id, organization_id=child.id,
          metadata={"amount_irr": order.amount_irr, "credit_limit_irr": order.credit_limit_irr,
                    "price_per_gib_irr": order.price_per_gib_irr, "panel_id": panel.id, "slug": child.slug,
                    "bound": bool(order.pg_admin_id)})
    enqueue(s, "panel_order.approved", order.id, {"organization_id": child.id, "telegram_actor_id": order.actor_id})
    return {"id": order.id, "status": order.status, "organization_id": child.id, "bound": bool(order.pg_admin_id),
            "panel_name": panel.name, "balance_irr": balance(s, child.id)}


@router.post("/v1/webapp/orders/{order_id}/approve")
def approve_order(order_id: str, x: OrderApproveIn = OrderApproveIn(), identity=Depends(web_identity),
                  s: Session = Depends(db)):
    reviewer, _, organization = manager(s, identity.user_id, "decide_orders")
    order = locked_order(s, order_id, "approved")
    result = approve_panel_order(s, order, reviewer.id, organization.id, x)
    s.commit()
    return result


@router.post("/v1/webapp/panels/{panel_id}/saleable")
def set_panel_saleable(panel_id: str, saleable: bool = True, identity=Depends(web_identity), s: Session = Depends(db)):
    """Put a server on the customer order list, or take it off again."""
    actor, _, organization = manager(s, identity.user_id, "manage_servers")
    panel = owned_panel(s, panel_id, organization.id)
    panel.saleable = saleable
    audit(s, "panel.saleable", "panel", panel.id, actor_id=actor.id, organization_id=organization.id,
          metadata={"saleable": saleable})
    s.commit()
    return {"id": panel.id, "saleable": panel.saleable}


# --------------------------------------------------------------------------------------
# The plan catalog: published prices a customer can buy without negotiating
# --------------------------------------------------------------------------------------

class PlanCategoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=80)
    description: str = Field(default="", max_length=500)


class PlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    panel_id: str
    category_id: str | None = None
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=1000)
    price_per_gib_irr: int = Field(gt=0, le=10_000_000_000_000)
    daily_gib: int = Field(gt=0, le=100_000)
    duration_days: int = Field(gt=0, le=3650)
    user_count: int = Field(gt=0, le=100_000)
    credit_limit_irr: int = Field(default=0, ge=0)
    payment_instructions: str = Field(default="", max_length=2000)


def catalog_shelf(s: Session, category_id: str, organization_id: str) -> PlanCategory:
    category = s.get(PlanCategory, category_id)
    if not category or category.organization_id != organization_id:
        raise HTTPException(404, "category not found")
    return category


@router.get("/v1/webapp/catalog")
def web_catalog(identity=Depends(web_identity), s: Session = Depends(db)):
    _, _, organization = manager(s, identity.user_id, "manage_catalog")
    categories = s.scalars(select(PlanCategory).where(PlanCategory.organization_id == organization.id)
                           .order_by(PlanCategory.name)).all()
    panels = {p.id: p.name for p in s.scalars(select(Panel).join(PanelOwner, PanelOwner.panel_id == Panel.id)
                                              .where(PanelOwner.organization_id == organization.id)).all()}
    plans = s.scalars(select(Plan).where(Plan.organization_id == organization.id)
                      .order_by(Plan.price_per_gib_irr)).all()
    return {"categories": [{"id": c.id, "name": c.name, "description": c.description, "status": c.status,
                            "plans": sum(1 for p in plans if p.category_id == c.id)} for c in categories],
            "plans": [plan_row(p, panels.get(p.panel_id)) for p in plans]}


def create_plan_category(s: Session, *, organization_id: str, actor_id: str, x: PlanCategoryIn) -> PlanCategory:
    if s.scalar(select(PlanCategory.id).where(PlanCategory.organization_id == organization_id,
                                              PlanCategory.name == x.name)):
        raise HTTPException(409, "یک دسته با همین نام وجود دارد")
    category = PlanCategory(organization_id=organization_id, name=x.name, description=x.description)
    s.add(category)
    audit(s, "plan_category.create", "plan_category", category.id, actor_id=actor_id, organization_id=organization_id,
          metadata={"name": x.name})
    return category


@router.post("/v1/webapp/plan-categories")
def web_create_category(x: PlanCategoryIn, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id, "manage_catalog")
    category = create_plan_category(s, organization_id=organization.id, actor_id=actor.id, x=x)
    s.commit()
    return {"id": category.id, "name": category.name, "status": category.status}


@router.put("/v1/webapp/plan-categories/{category_id}")
def web_update_category(category_id: str, x: PlanCategoryIn, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id, "manage_catalog")
    category = catalog_shelf(s, category_id, organization.id)
    taken = s.scalar(select(PlanCategory.id).where(PlanCategory.organization_id == organization.id,
                                                  PlanCategory.name == x.name, PlanCategory.id != category.id))
    if taken:
        raise HTTPException(409, "یک دسته با همین نام وجود دارد")
    category.name, category.description = x.name, x.description
    audit(s, "plan_category.update", "plan_category", category.id, actor_id=actor.id, organization_id=organization.id,
          metadata={"name": x.name})
    s.commit()
    return {"id": category.id, "name": category.name, "status": category.status}


def set_catalog_status(s: Session, *, organization_id: str, actor_id: str, kind: str, target_id: str,
                       status: str) -> str:
    """Closing a shelf hides every plan on it; the plans themselves stay as they were."""
    if status not in ("active", "inactive"):
        raise HTTPException(422, "status must be active or inactive")
    action, table = {"category": ("plan_category.status", "plan_category"), "plan": ("plan.status", "plan")}[kind]
    row = catalog_shelf(s, target_id, organization_id) if kind == "category" else catalog_plan(s, target_id,
                                                                                                organization_id)
    row.status = status
    audit(s, action, table, target_id, actor_id=actor_id, organization_id=organization_id, metadata={"status": status})
    return status


@router.post("/v1/webapp/plan-categories/{category_id}/status")
def web_category_status(category_id: str, status: str = "active", identity=Depends(web_identity),
                        s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id, "manage_catalog")
    status = set_catalog_status(s, organization_id=organization.id, actor_id=actor.id, kind="category",
                                target_id=category_id, status=status)
    s.commit()
    return {"id": category_id, "status": status}


@router.delete("/v1/webapp/plan-categories/{category_id}")
def web_delete_category(category_id: str, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id, "manage_catalog")
    category = catalog_shelf(s, category_id, organization.id)
    if s.scalar(select(Plan.id).where(Plan.category_id == category.id)):
        raise HTTPException(409, "این دسته هنوز پلن دارد؛ اول پلن‌ها را منتقل یا حذف کنید")
    s.delete(category)
    audit(s, "plan_category.delete", "plan_category", category_id, actor_id=actor.id, organization_id=organization.id)
    s.commit()
    return {"deleted": category_id}


def catalog_plan(s: Session, plan_id: str, organization_id: str) -> Plan:
    plan = s.get(Plan, plan_id)
    if not plan or plan.organization_id != organization_id:
        raise HTTPException(404, "plan not found")
    return plan


def validated_plan(s: Session, x: PlanIn, organization_id: str) -> Panel:
    """A plan may only sit on a server the seller owns, and on a shelf of their own."""
    if x.category_id:
        catalog_shelf(s, x.category_id, organization_id)
    return owned_panel(s, x.panel_id, organization_id)


def create_catalog_plan(s: Session, *, organization_id: str, actor_id: str, x: PlanIn) -> Plan:
    panel = validated_plan(s, x, organization_id)
    plan = Plan(organization_id=organization_id, panel_id=panel.id, category_id=x.category_id, name=x.name,
                description=x.description, price_per_gib_irr=x.price_per_gib_irr, daily_gib=x.daily_gib,
                duration_days=x.duration_days, user_count=x.user_count, credit_limit_irr=x.credit_limit_irr,
                payment_instructions=x.payment_instructions)
    s.add(plan)
    audit(s, "plan.create", "plan", plan.id, actor_id=actor_id, organization_id=organization_id,
          metadata={"panel_id": panel.id, "name": x.name, "amount_irr": plan.amount_irr})
    return plan


@router.post("/v1/webapp/plans")
def web_create_plan(x: PlanIn, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id, "manage_catalog")
    plan = create_catalog_plan(s, organization_id=organization.id, actor_id=actor.id, x=x)
    s.commit()
    return plan_row(plan, s.get(Panel, plan.panel_id).name)


@router.put("/v1/webapp/plans/{plan_id}")
def web_update_plan(plan_id: str, x: PlanIn, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id, "manage_catalog")
    plan = catalog_plan(s, plan_id, organization.id)
    panel = validated_plan(s, x, organization.id)
    plan.panel_id, plan.category_id, plan.name, plan.description = panel.id, x.category_id, x.name, x.description
    plan.price_per_gib_irr, plan.daily_gib, plan.duration_days = x.price_per_gib_irr, x.daily_gib, x.duration_days
    plan.user_count, plan.credit_limit_irr, plan.payment_instructions = x.user_count, x.credit_limit_irr, x.payment_instructions
    audit(s, "plan.update", "plan", plan.id, actor_id=actor.id, organization_id=organization.id,
          metadata={"panel_id": panel.id, "name": x.name, "amount_irr": plan.amount_irr})
    s.commit()
    return plan_row(plan, panel.name)


@router.post("/v1/webapp/plans/{plan_id}/status")
def web_plan_status(plan_id: str, status: str = "active", identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id, "manage_catalog")
    status = set_catalog_status(s, organization_id=organization.id, actor_id=actor.id, kind="plan",
                                target_id=plan_id, status=status)
    s.commit()
    return {"id": plan_id, "status": status}


@router.delete("/v1/webapp/plans/{plan_id}")
def web_delete_plan(plan_id: str, identity=Depends(web_identity), s: Session = Depends(db)):
    """An order keeps its price history, so a plan that was ever bought is only ever closed."""
    actor, _, organization = manager(s, identity.user_id, "manage_catalog")
    plan = catalog_plan(s, plan_id, organization.id)
    if s.scalar(select(PanelOrder.id).where(PanelOrder.plan_id == plan.id)):
        raise HTTPException(409, "این پلن سفارش دارد؛ فقط می‌توان آن را بست")
    s.delete(plan)
    audit(s, "plan.delete", "plan", plan_id, actor_id=actor.id, organization_id=organization.id)
    s.commit()
    return {"deleted": plan_id}
