"""Shared pytest fixtures: boots a real PostgreSQL instance and applies every migration."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pathlib
import time
from urllib.parse import urlencode

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _database_url() -> str:
    # CI points this at a real PostgreSQL service container; pgserver only ships Windows binaries.
    supplied = os.environ.get("PYTEST_DATABASE_URL")
    if supplied:
        return supplied if "+" in supplied else supplied.replace("postgresql://", "postgresql+psycopg://")

    import pgserver

    data_dir = ROOT.parent / ".pgdata-control-plane-test"
    return pgserver.get_server(str(data_dir), cleanup_mode=None).get_uri().replace("postgresql://", "postgresql+psycopg://")


DATABASE_URL = _database_url()

os.environ.update(
    DATABASE_URL=DATABASE_URL,
    MIGRATIONS_DIR=str(ROOT / "migrations"),
    CONTROL_API_KEY="c" * 40,
    APP_ENCRYPTION_KEY="a" * 44,
    TELEGRAM_BOT_TOKEN="123456:TEST-BOT-TOKEN",
    TELEGRAM_WEBHOOK_SECRET="w" * 40,
    ROOT_TELEGRAM_ID="100001",
    INITIAL_SETUP_TOKEN="s" * 40,
    WEBAPP_URL="https://panel.example.test/app/",
    REDIS_URL="redis://127.0.0.1:6379/0",
    USAGE_COEFFICIENT="1",
    METRICS_TOKEN="m" * 32,
    # The guard rails are exercised on their own; the suite must not trip over them.
    RATE_LIMIT_WRITE_PER_MINUTE="100000",
    RATE_LIMIT_PER_MINUTE="100000",
)

API_KEY_HEADERS = {"X-Control-Key": os.environ["CONTROL_API_KEY"]}


def sign_init_data(user_id: int, auth_date: int | None = None, token: str | None = None) -> str:
    values = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AQ测试",
        "user": json.dumps({"id": user_id, "first_name": "Test", "is_premium": False}, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
    secret = hmac.new(b"WebAppData", (token or os.environ["TELEGRAM_BOT_TOKEN"]).encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def as_user(user_id: int) -> dict:
    return {"X-Telegram-Init-Data": sign_init_data(user_id)}


@pytest.fixture(scope="session")
def engine():
    from control_plane.app import ENGINE, Base
    from control_plane.migrate import run_migrations

    Base.metadata.create_all(ENGINE)
    run_migrations(ENGINE)
    return ENGINE


def _clear(session) -> None:
    from sqlalchemy import text

    rows = session.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename<>'schema_migrations'")
    ).scalars().all()
    if rows:
        quoted = ", ".join(f'"{name}"' for name in rows)
        session.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))
        session.commit()


@pytest.fixture
def session(engine):
    from control_plane.app import SessionLocal

    with SessionLocal() as active:
        _clear(active)
        yield active


@pytest.fixture
def client(engine):
    from fastapi.testclient import TestClient

    from control_plane.main import app
    from control_plane.observability import anon_limiter, write_limiter

    write_limiter.reset(); anon_limiter.reset()
    with TestClient(app) as test_client:
        test_client.headers.update(API_KEY_HEADERS)
        yield test_client


@pytest.fixture
def make_org(session):
    from control_plane.app import Closure, Contract, Organization, account

    counter = {"n": 0}

    def build(parent=None, price_per_gib_irr: int = 5000, credit_limit_irr: int = 0):
        counter["n"] += 1
        org = Organization(
            name=f"Org {counter['n']}",
            slug=f"org-{counter['n']}",
            parent_id=parent.id if parent else None,
            credit_limit_irr=credit_limit_irr,
        )
        session.add(org)
        session.flush()
        session.add(Closure(ancestor_id=org.id, descendant_id=org.id, depth=0))
        if parent:
            from sqlalchemy import select

            for edge in session.scalars(select(Closure).where(Closure.descendant_id == parent.id)).all():
                session.add(Closure(ancestor_id=edge.ancestor_id, descendant_id=org.id, depth=edge.depth + 1))
            session.add(Contract(parent_id=parent.id, child_id=org.id, price_per_gib_irr=price_per_gib_irr))
        account(session, org.id)
        session.commit()
        return org

    return build


@pytest.fixture
def make_actor(session):
    from control_plane.app import Actor, Membership

    def build(telegram_id: int, organization, role: str = "reseller_admin") -> Actor:
        actor = Actor(telegram_id=telegram_id, display_name=f"Actor {telegram_id}")
        session.add(actor)
        session.flush()
        session.add(Membership(organization_id=organization.id, actor_id=actor.id, role=role))
        session.commit()
        return actor

    return build


@pytest.fixture
def make_binding(session):
    from control_plane.app import Binding, Panel

    def build(organization, panel=None, pg_admin_id: int = 7) -> Binding:
        panel = panel or Panel(name="p", base_url="https://panel.example.test", verify_tls=True)
        session.add(panel)
        session.flush()
        binding = Binding(organization_id=organization.id, panel_id=panel.id, pg_admin_id=pg_admin_id, username="admin")
        session.add(binding)
        session.commit()
        return binding

    return build


@pytest.fixture(autouse=True)
def utc_now_freeze():
    yield
