import hmac
import os
from decimal import Decimal

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .app import (
    Actor, Binding, Closure, Membership, Organization, Panel, PanelOwner, SystemSetting,
    account, app, audit, db, membership_for, put_setting, validate_panel_url, web_identity,
)
from .pasarguard import Client
from .secrets import encrypt_secret, resolve_secret

app.version = "0.8.0"

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


class AdminLimitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit_use_in_bytes: int = Field(ge=0)


class CoefficientIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    usage_coefficient: Decimal = Field(gt=Decimal("0"), le=Decimal("1000"))


def optional_membership(s: Session, telegram_id: int):
    actor = s.scalar(select(Actor).where(Actor.telegram_id == telegram_id, Actor.status == "active"))
    if not actor:
        return None
    membership = s.scalar(select(Membership).where(Membership.actor_id == actor.id, Membership.status == "active"))
    return (actor, membership, s.get(Organization, membership.organization_id)) if membership else None

def manager(s: Session, telegram_id: int):
    actor, membership, organization = membership_for(s, telegram_id)
    if membership.role not in ("reseller_admin", "operator"):
        raise HTTPException(403, "manager role required")
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

@app.get("/v1/webapp/session")
def web_session(identity=Depends(web_identity), s: Session = Depends(db)):
    linked = optional_membership(s, identity.user_id)
    if not linked:
        return {"needs_setup": True, "telegram_id": identity.user_id}
    actor, membership, organization = linked
    return {"needs_setup": False, "actor": {"name": actor.display_name, "role": membership.role}, "organization": {"id": organization.id, "name": organization.name}}

@app.post("/v1/onboarding/bootstrap")
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

@app.get("/v1/webapp/panels")
def web_panels(identity=Depends(web_identity), s: Session = Depends(db)):
    _, _, organization = manager(s, identity.user_id)
    rows = s.scalars(select(Panel).join(PanelOwner, PanelOwner.panel_id == Panel.id).where(PanelOwner.organization_id == organization.id).order_by(Panel.name)).all()
    return [{"id": p.id, "name": p.name, "base_url": p.base_url, "status": p.status} for p in rows]

@app.post("/v1/webapp/panels")
def web_create_panel(x: WebPanelIn, identity=Depends(web_identity), s: Session = Depends(db)):
    _, _, organization = manager(s, identity.user_id)
    validate_panel_url(x.base_url)
    url = x.base_url.rstrip("/")
    try:
        probe = Client(url, x.api_key, x.owner_username, x.owner_password, True).nodes()
    except Exception as exc:
        # The upstream failure can echo the request URL; never let that reach the client log.
        raise HTTPException(422, "PasarGuard connection failed: the server did not answer the test request") from exc
    panel = Panel(name=x.name, base_url=url, api_key_ref=encrypt_secret(x.api_key), owner_user_ref=encrypt_secret(x.owner_username), owner_pass_ref=encrypt_secret(x.owner_password), usage_coefficient=x.usage_coefficient, verify_tls=True)
    s.add(panel); s.flush(); s.add(PanelOwner(panel_id=panel.id, organization_id=organization.id))
    if x.pg_admin_id:
        if s.scalar(select(Binding.id).where(Binding.organization_id == organization.id)):
            raise HTTPException(409, "this organization already has a billing binding")
        s.add(Binding(organization_id=organization.id, panel_id=panel.id, pg_admin_id=x.pg_admin_id, username=x.admin_username or str(x.pg_admin_id)))
    audit(s, "panel.create", "panel", panel.id, organization_id=organization.id, metadata={"base_url": panel.base_url, "usage_coefficient": str(panel.usage_coefficient)})
    s.commit()
    nodes = probe.get("nodes", probe if isinstance(probe, list) else [])
    return {"id": panel.id, "name": panel.name, "status": panel.status, "nodes_detected": len(nodes) if isinstance(nodes, list) else 0}

@app.get("/v1/webapp/panels/{panel_id}/nodes")
def web_panel_nodes(panel_id: str, identity=Depends(web_identity), s: Session = Depends(db)):
    _, _, organization = manager(s, identity.user_id); panel = owned_panel(s, panel_id, organization.id)
    nodes = panel_operation(s, panel, organization.id, "panel.nodes.read", lambda c: c.nodes())
    return {"panel_id": panel.id, "nodes": _unwrap(nodes, "nodes")}

@app.post("/v1/webapp/panels/{panel_id}/nodes/{node_id}/reconnect")
def web_reconnect_node(panel_id: str, node_id: int, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id); panel = owned_panel(s, panel_id, organization.id)
    result = panel_operation(s, panel, organization.id, "node.reconnect", lambda c: c.reconnect_node(node_id), actor.id, {"node_id": node_id})
    return {"ok": True, "result": result}

@app.get("/v1/webapp/panels/{panel_id}/nodes/{node_id}/status")
def web_node_status(panel_id: str, node_id: int, identity=Depends(web_identity), s: Session = Depends(db)):
    _, _, organization = manager(s, identity.user_id); panel = owned_panel(s, panel_id, organization.id)
    return panel_operation(s, panel, organization.id, "node.status.read", lambda c: c.node_status(node_id))

@app.post("/v1/webapp/panels/{panel_id}/nodes/{node_id}/{enabled}")
def web_set_node_enabled(panel_id: str, node_id: int, enabled: str, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id); panel = owned_panel(s, panel_id, organization.id)
    flag = _enabled_flag(enabled)
    result = panel_operation(s, panel, organization.id, f"node.{'enable' if flag else 'disable'}",
                             lambda c: c.set_node_enabled(node_id, flag), actor.id, {"node_id": node_id})
    return {"ok": True, "enabled": flag, "result": result}

@app.get("/v1/webapp/panels/{panel_id}/users")
def web_panel_users(panel_id: str, offset: int = 0, limit: int = 50, identity=Depends(web_identity), s: Session = Depends(db)):
    _, _, organization = manager(s, identity.user_id); panel = owned_panel(s, panel_id, organization.id)
    binding = org_binding(s, organization.id, panel.id)
    if limit < 1 or limit > 200:
        raise HTTPException(422, "limit must be between 1 and 200")
    users = panel_operation(s, panel, organization.id, "user.list", lambda c: c.users(binding.pg_admin_id, offset, limit))
    return {"panel_id": panel.id, "admin_id": binding.pg_admin_id, "offset": offset, "limit": limit,
            "users": _unwrap(users, "users"), "total": users.get("total") if isinstance(users, dict) else None}

@app.post("/v1/webapp/panels/{panel_id}/users/{user_id}/reset-data")
def web_reset_user_data(panel_id: str, user_id: int, identity=Depends(web_identity), s: Session = Depends(db)):
    """Clears a subscriber's counters on the panel; destructive, so it is always audited."""
    actor, _, organization = manager(s, identity.user_id); panel = owned_panel(s, panel_id, organization.id)
    result = panel_operation(s, panel, organization.id, "user.reset_data", lambda c: c.reset_user_data(user_id), actor.id, {"user_id": user_id})
    return {"ok": True, "result": result}

@app.post("/v1/webapp/panels/{panel_id}/users/{user_id}/{enabled}")
def web_set_user_enabled(panel_id: str, user_id: int, enabled: str, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id); panel = owned_panel(s, panel_id, organization.id)
    flag = _enabled_flag(enabled)
    result = panel_operation(s, panel, organization.id, f"user.{'enable' if flag else 'disable'}",
                             lambda c: c.set_user_enabled(user_id, flag), actor.id, {"user_id": user_id})
    return {"ok": True, "enabled": flag, "result": result}

@app.post("/v1/webapp/panels/{panel_id}/limit")
def web_set_admin_limit(panel_id: str, x: AdminLimitIn, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id); panel = owned_panel(s, panel_id, organization.id)
    binding = org_binding(s, organization.id, panel.id)
    result = panel_operation(s, panel, organization.id, "admin.limit",
                             lambda c: c.set_admin_limit(binding.pg_admin_id, x.limit_use_in_bytes), actor.id,
                             {"pg_admin_id": binding.pg_admin_id, "limit_use_in_bytes": x.limit_use_in_bytes})
    return {"ok": True, "result": result}

@app.post("/v1/webapp/panels/{panel_id}/coefficient")
def web_set_usage_coefficient(panel_id: str, x: CoefficientIn, identity=Depends(web_identity), s: Session = Depends(db)):
    actor, _, organization = manager(s, identity.user_id)
    if identity.user_id != int(os.getenv("ROOT_TELEGRAM_ID", "0") or 0):
        raise HTTPException(403, "only the system administrator may change the billing coefficient")
    panel = owned_panel(s, panel_id, organization.id)
    panel.usage_coefficient = x.usage_coefficient
    audit(s, "panel.coefficient", "panel", panel.id, actor_id=actor.id, organization_id=organization.id,
          metadata={"usage_coefficient": str(x.usage_coefficient)})
    s.commit()
    s.refresh(panel)
    return {"id": panel.id, "usage_coefficient": str(panel.usage_coefficient)}
