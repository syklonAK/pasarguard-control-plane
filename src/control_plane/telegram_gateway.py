import hmac, json, os
from fastapi import FastAPI, Header, HTTPException, Request
from redis.asyncio import Redis

app=FastAPI(title="Telegram Gateway",version="0.2.0")
redis=Redis.from_url(os.getenv("REDIS_URL","redis://redis:6379/0"),decode_responses=True)

@app.get("/health")
async def health():
    return {"status":"ok","redis":bool(await redis.ping())}

@app.post("/telegram/webhook")
async def webhook(request:Request,x_telegram_bot_api_secret_token:str=Header(default="")):
    secret=os.getenv("TELEGRAM_WEBHOOK_SECRET","")
    if not secret or not hmac.compare_digest(secret,x_telegram_bot_api_secret_token):
        raise HTTPException(401,"invalid webhook secret")
    update=await request.json()
    update_id=str(update.get("update_id",""))
    if not update_id:raise HTTPException(422,"missing update_id")
    dedupe=f"tg:update:{update_id}"
    if not await redis.set(dedupe,"1",ex=86400,nx=True):return {"ok":True,"duplicate":True}
    try:
        await redis.xadd("telegram_updates",{"update":json.dumps(update,separators=(",",":"))},maxlen=100000,approximate=True)
    except Exception:
        # A lost queue write must not consume the update id: Telegram retries, and the retry
        # has to be accepted rather than filtered as a duplicate.
        await redis.delete(dedupe)
        raise
    return {"ok":True}
