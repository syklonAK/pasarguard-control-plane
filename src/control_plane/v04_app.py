import hmac
import os
from decimal import Decimal

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
    try:
        result = panel_client(panel).nodes()
    except Exception as exc:
        raise HTTPException(502, f"PasarGuard request failed: {exc}") from exc
    nodes = result.get("nodes", result if isinstance(result, list) else [])
    return {"panel_id": panel.id, "nodes": nodes if isinstance(nodes, list) else []}

@app.post("/v1/webapp/panels/{panel_id}/nodes/{node_id}/reconnect")
def web_reconnect_node(panel_id: str, node_id: int, identity=Depends(web_identity), s: Session = Depends(db)):
    _, _, organization = manager(s, identity.user_id); panel = owned_panel(s, panel_id, organization.id)
    try:
        result = panel_client(panel).reconnect_node(node_id)
    except Exception as exc:
        raise HTTPException(502, f"PasarGuard reconnect failed: {exc}") from exc
    return {"ok": True, "result": result}
