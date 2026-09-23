import os
from fastapi import Request
from fastapi.responses import JSONResponse
from .telegram_auth import TelegramAuthError, verify_init_data
from .v04_app import app

def configured_root_id():
    value=os.getenv("ROOT_TELEGRAM_ID","")
    return int(value) if value.isdigit() else None

@app.middleware("http")
async def protect_initial_bootstrap(request:Request,call_next):
    if request.method=="POST" and request.url.path=="/v1/onboarding/bootstrap":
        root=configured_root_id()
        if root is None:return JSONResponse({"detail":"ROOT_TELEGRAM_ID is not configured"},status_code=503)
        try:identity=verify_init_data(request.headers.get("X-Telegram-Init-Data",""),os.getenv("TELEGRAM_BOT_TOKEN",""),max_age_seconds=3600)
        except TelegramAuthError as exc:return JSONResponse({"detail":str(exc)},status_code=401)
        if identity.user_id!=root:return JSONResponse({"detail":"only the configured Telegram administrator can initialize this workspace"},status_code=403)
    return await call_next(request)
