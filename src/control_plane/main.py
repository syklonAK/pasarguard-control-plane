"""The single ASGI entrypoint: ``control_plane.main:app``.

Composition is deliberately flat. :mod:`control_plane.api` serves the machine-to-machine
control API behind ``X-Control-Key``, :mod:`control_plane.web_api` serves everything a human
sees behind validated Telegram initData, and both share the domain kernel in
:mod:`control_plane.app`.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from . import observability as metrics
from .api import router as control_router
from .app import SessionLocal, configured_root_id, lifespan, now
from .telegram_auth import TelegramAuthError, declared_user_id, verify_init_data
from .web_api import router as web_router

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'self'",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SLOW_REQUEST_SECONDS = float(os.getenv("SLOW_REQUEST_SECONDS", "2"))
# A proxy in front of the API is the only party allowed to name the real client.
TRUST_PROXY = os.getenv("TRUST_PROXY_HEADERS", "0") == "1"


def _caller_key(request: Request) -> str:
    """Rate budget follows the authenticated identity, not a spoofable header.

    Hashing the whole initData header would hand a fresh budget to the same caller every time
    the WebApp re-signs with a new ``auth_date``, which is every few seconds.
    """
    peer = ""
    if TRUST_PROXY:
        peer = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if not peer:
        peer = request.client.host if request.client else "unknown"
    declared = declared_user_id(request.headers.get("x-telegram-init-data", ""))
    if declared is not None:
        return f"{peer}:telegram-{declared}"
    # Machine callers authenticate by API key, so the budget follows the credential.
    key = request.headers.get("x-control-key", "")
    return f"{peer}:key-{hashlib.sha256(key.encode()).hexdigest()[:16]}" if key else peer


def create_app() -> FastAPI:
    metrics.configure_logging()
    application = FastAPI(title="PasarGuard B2B Control Plane", version="0.8.0", lifespan=lifespan,
                          docs_url="/docs" if os.getenv("EXPOSE_DOCS") == "1" else None,
                          redoc_url=None, openapi_url="/openapi.json" if os.getenv("EXPOSE_DOCS") == "1" else None)

    @application.middleware("http")
    async def guard_rails(request: Request, call_next):
        started = time.monotonic()
        request.state.request_id = request.headers.get("x-request-id", "")[:64] or str(uuid.uuid4())
        if request.method in WRITE_METHODS:
            allowed, limiter = metrics.write_limiter.allow(_caller_key(request)), metrics.write_limiter
        else:
            allowed, limiter = metrics.anon_limiter.allow(_caller_key(request)), metrics.anon_limiter
        if not allowed:
            metrics.inc("http_rate_limited_total", {"path": request.url.path})
            return JSONResponse({"detail": "درخواست‌های شما بیش از حد مجاز است. کمی صبر کنید."},
                                status_code=429, headers={"Retry-After": str(max(1, limiter.window))})
        try:
            response = await call_next(request)
        except Exception:
            metrics.inc("http_errors_total", {"path": request.url.path})
            raise
        duration = time.monotonic() - started
        # The route template keeps label cardinality bounded, unlike a raw path with ids in it.
        route = request.scope.get("route")
        label = getattr(route, "path", None) or "<unmatched>"
        metrics.observe_latency(request.method, label, duration)
        metrics.inc("http_responses_total", {"path": label, "status": str(response.status_code)})
        if response.status_code >= 400 or duration > SLOW_REQUEST_SECONDS:
            # Only failures and slow calls are logged, so a busy node keeps a readable journal.
            metrics.log("warning", "request", request_id=request.state.request_id, path=label,
                        status=response.status_code, duration_ms=int(duration * 1000))
        response.headers["X-Request-Id"] = request.state.request_id
        for header, value in SECURITY_HEADERS.items():
            response.headers[header] = value
        return response

    @application.middleware("http")
    async def protect_initial_bootstrap(request: Request, call_next):
        """Only the configured Telegram administrator may claim the empty workspace."""
        if request.method == "POST" and request.url.path == "/v1/onboarding/bootstrap":
            root = configured_root_id()
            if root is None:
                return JSONResponse({"detail": "ROOT_TELEGRAM_ID is not configured"}, status_code=503)
            try:
                identity = verify_init_data(request.headers.get("X-Telegram-Init-Data", ""),
                                            os.getenv("TELEGRAM_BOT_TOKEN", ""), max_age_seconds=3600)
            except TelegramAuthError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=401)
            if identity.user_id != root:
                return JSONResponse({"detail": "only the configured Telegram administrator can initialize this workspace"},
                                    status_code=403)
        return await call_next(request)

    @application.get("/health")
    def health():
        """Fail loudly: a drifted ledger or an unreachable database must not report 200."""
        try:
            with SessionLocal() as session:
                drifted, drift_total = session.execute(
                    text("SELECT count(*), COALESCE(sum(drift),0) FROM ledger_imbalance")).one()
        except Exception:
            raise HTTPException(503, {"status": "degraded", "database": False}) from None
        metrics.gauge("ledger_drift_accounts", int(drifted))
        if drifted:
            raise HTTPException(503, {"status": "degraded", "database": True,
                                      "ledger_drift_accounts": int(drifted), "ledger_drift_irr": int(drift_total)})
        return {"status": "ok", "time": now()}

    @application.get("/metrics")
    def scrape(x_metrics_token: str = Header(default="")):
        """Metrics are fail-closed: METRICS_TOKEN must be configured, and it travels as a header."""
        expected = os.getenv("METRICS_TOKEN", "")
        if not expected:
            raise HTTPException(503, "METRICS_TOKEN is not configured")
        if not hmac.compare_digest(expected, x_metrics_token):
            raise HTTPException(401, "invalid metrics token")
        return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")

    application.include_router(control_router)
    application.include_router(web_router)
    return application


app = create_app()
