import asyncio, json, os
import httpx
from redis.asyncio import Redis

STREAM="telegram_updates"; GROUP="telegram-workers"
MENU={"keyboard":[[{"text":"📊 Dashboard"},{"text":"🛒 Wholesale"}],[{"text":"👥 Resellers"},{"text":"🧩 Services"}],[{"text":"💳 Billing"},{"text":"🛟 Support"}]],"resize_keyboard":True}

async def send(chat_id:int,text:str):
    token=os.environ["TELEGRAM_BOT_TOKEN"]
    async with httpx.AsyncClient(timeout=10) as c:
        url="https:"+"//"+"api.telegram.org"+"/bot"+token+"/sendMessage"
        r=await c.post(url,json={"chat_id":chat_id,"text":text,"reply_markup":MENU});r.raise_for_status()

async def main():
    redis=Redis.from_url(os.getenv("REDIS_URL","redis://redis:6379/0"),decode_responses=True)
    try:await redis.xgroup_create(STREAM,GROUP,id="0",mkstream=True)
    except Exception:pass
    consumer=os.getenv("HOSTNAME","telegram-1")
    while True:
        rows=await redis.xreadgroup(GROUP,consumer,{STREAM:">"},count=50,block=5000)
        for _,items in rows:
            for message_id,data in items:
                try:
                    u=json.loads(data["update"]);m=u.get("message") or {};chat=(m.get("chat") or {}).get("id")
                    if chat:await send(chat,"Welcome to your wholesale control panel." if m.get("text")=="/start" else "Your request has been received.")
                    await redis.xack(STREAM,GROUP,message_id)
                except Exception as exc:print(f"telegram message {message_id}: {exc}",flush=True)

if __name__=="__main__":asyncio.run(main())
