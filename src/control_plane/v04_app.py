import hmac
import os
from datetime import datetime
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .app import (
    Account, Actor, Base, Binding, Closure, ENGINE, Membership, Organization,
    Panel, account, app, db, membership_for, web_identity,
)
from .migrate import run_migrations
from .pasarguard import Client
from .secrets import encrypt_secret, resolve_secret

app.version = "0.4.0"

class PanelOwner(Base):
    __tablename__ = "panel_owners"
    panel_id: Mapped[str] = mapped_column(ForeignKey("panels.id"), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)

class SystemSetting(Base):
    __tablename__ = "system_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

class BootstrapIn(BaseModel):
    business_name: str = Field(min_length=2, max_length=160)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$")
    setup_token: str = Field(min_length=32, max_length=256)

class WebPanelIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    base_url: str = Field(min_length=10, max_length=500)
    api_key: str = Field(min_length=8, max_length=500)
    owner_username: str | None = Field(default=None, max_length=200)
    owner_password: str | None = Field(default=None, max_length=500)
    pg_admin_id: int | None = Field(default=None, gt=0)
    admin_username: str | None = Field(default=None, max_length=80)

@app.on_event("startup")
def migrate_on_startup():
    Base.metadata.create_all(ENGINE)
    run_migrations(ENGINE)

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
    s.add(SystemSetting(key="bootstrap_complete", value=organization.id)); s.commit()
    return {"organization_id": organization.id, "actor_id": actor.id}

@app.get("/v1/webapp/panels")
def web_panels(identity=Depends(web_identity), s: Session = Depends(db)):
    _, _, organization = manager(s, identity.user_id)
    rows = s.scalars(select(Panel).join(PanelOwner, PanelOwner.panel_id == Panel.id).where(PanelOwner.organization_id == organization.id).order_by(Panel.name)).all()
    return [{"id": p.id, "name": p.name, "base_url": p.base_url, "status": p.status} for p in rows]

@app.post("/v1/webapp/panels")
def web_create_panel(x: WebPanelIn, identity=Depends(web_identity), s: Session = Depends(db)):
    _, _, organization = manager(s, identity.user_id)
    if not x.base_url.startswith("https://"):
        raise HTTPException(422, "server URL must use HTTPS")
    try:
        probe = Client(x.base_url.rstrip("/"), x.api_key, x.owner_username, x.owner_password, True).nodes()
    except Exception as exc:
        raise HTTPException(422, f"PasarGuard connection failed: {exc}") from exc
    panel = Panel(name=x.name, base_url=x.base_url.rstrip("/"), api_key_ref=encrypt_secret(x.api_key), owner_user_ref=encrypt_secret(x.owner_username), owner_pass_ref=encrypt_secret(x.owner_password), verify_tls=True)
    s.add(panel); s.flush(); s.add(PanelOwner(panel_id=panel.id, organization_id=organization.id))
    if x.pg_admin_id:
        if s.scalar(select(Binding.id).where(Binding.organization_id == organization.id)):
            raise HTTPException(409, "this organization already has a billing binding")
        s.add(Binding(organization_id=organization.id, panel_id=panel.id, pg_admin_id=x.pg_admin_id, username=x.admin_username or str(x.pg_admin_id)))
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
