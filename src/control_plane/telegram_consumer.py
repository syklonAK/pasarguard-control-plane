import asyncio,json,os
import httpx
from redis.asyncio import Redis
STREAM="telegram_updates";GROUP="telegram-workers"
MENU={"keyboard":[[{"text":"🏠 Home"},{"text":"🖥 Servers"}],[{"text":"👥 Resellers"},{"text":"💳 Billing"}],[{"text":"🛟 Support"},{"text":"🌐 Open Web App"}]],"resize_keyboard":True,"is_persistent":True}
def webapp_button():
 url=os.getenv("WEBAPP_URL","")
 return {"inline_keyboard":[[{"text":"Open Control Panel","web_app":{"url":url}}]]} if url else None
async def send(chat_id,text,reply_markup=None):
 token=os.environ["TELEGRAM_BOT_TOKEN"];url="https:"+"//"+"api.telegram.org"+"/bot"+token+"/sendMessage";payload={"chat_id":chat_id,"text":text}
 if reply_markup:payload["reply_markup"]=reply_markup
 async with httpx.AsyncClient(timeout=10) as c:r=await c.post(url,json=payload);r.raise_for_status()
async def handle(chat_id,text):
 value=(text or "").strip()
 if value.startswith("/start"):
  await send(chat_id,"Welcome. Use the menu below to manage your reseller business.",MENU)
  if webapp_button():await send(chat_id,"Open the secure panel to finish setup or manage your account.",webapp_button())
  return
 messages={"🏠 Home":"Your dashboard shows balance, usage, resellers and server health.","🖥 Servers":"Register PasarGuard servers and manage nodes from the Servers page.","👥 Resellers":"Create reseller accounts, pricing and credit limits from the Resellers page.","💳 Billing":"Review transactions and request credit from the Billing page.","🛟 Support":"Open Support for diagnostics and update instructions.","🌐 Open Web App":"Open your control panel below.","/dashboard":"Open your control panel below.","/servers":"Open the Servers page in your control panel.","/support":"Open Support in your control panel."}
 await send(chat_id,messages.get(value,"Choose an option or open the control panel."),webapp_button() or MENU)
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
     update=json.loads(data["update"]);message=update.get("message") or {};chat=(message.get("chat") or {}).get("id")
     if chat:await handle(chat,message.get("text",""))
     await redis.xack(STREAM,GROUP,message_id)
    except Exception as exc:print(f"telegram message {message_id}: {exc}",flush=True)
if __name__=="__main__":asyncio.run(main())
