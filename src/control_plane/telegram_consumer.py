import asyncio,json,os
import httpx
from redis.asyncio import Redis
STREAM="telegram_updates";GROUP="telegram-workers"
MENU={"keyboard":[[{"text":"🏠 خانه"},{"text":"🖥 سرورها"}],[{"text":"👥 نمایندگان"},{"text":"💳 امور مالی"}],[{"text":"🛟 پشتیبانی"},{"text":"🌐 ورود به پنل"}]],"resize_keyboard":True,"is_persistent":True}
def webapp_button():
 url=os.getenv("WEBAPP_URL","")
 return {"inline_keyboard":[[{"text":"ورود به پنل مدیریت","web_app":{"url":url}}]]} if url else None
async def send(chat_id,text,reply_markup=None):
 token=os.environ["TELEGRAM_BOT_TOKEN"];url="https:"+"//"+"api.telegram.org"+"/bot"+token+"/sendMessage";payload={"chat_id":chat_id,"text":text}
 if reply_markup:payload["reply_markup"]=reply_markup
 async with httpx.AsyncClient(timeout=10) as c:r=await c.post(url,json=payload);r.raise_for_status()
async def handle(chat_id,text):
 value=(text or "").strip()
 if value.startswith("/start"):
  await send(chat_id,"خوش آمدید. از منوی زیر می‌توانید کسب‌وکار و نمایندگان خود را مدیریت کنید.",MENU)
  if webapp_button():await send(chat_id,"برای تکمیل راه‌اندازی یا مدیریت حساب، وارد پنل امن شوید.",webapp_button())
  return
 messages={"🏠 خانه":"در داشبورد می‌توانید موجودی، مصرف، نمایندگان و وضعیت سرورها را مشاهده کنید.","🖥 سرورها":"ثبت سرور پاسارگارد و مدیریت نودها از بخش سرورها در پنل انجام می‌شود.","👥 نمایندگان":"ساخت نماینده، تعیین قیمت و مدیریت اعتبار از بخش نمایندگان انجام می‌شود.","💳 امور مالی":"تراکنش‌ها و درخواست افزایش اعتبار را از بخش امور مالی بررسی کنید.","🛟 پشتیبانی":"برای عیب‌یابی و راهنمای به‌روزرسانی وارد بخش پشتیبانی شوید.","🌐 ورود به پنل":"برای ورود به پنل مدیریت، دکمه زیر را بزنید.","/dashboard":"برای ورود به پنل مدیریت، دکمه زیر را بزنید.","/servers":"برای مدیریت سرورها و نودها وارد پنل شوید.","/support":"برای دریافت راهنما وارد بخش پشتیبانی پنل شوید."}
 await send(chat_id,messages.get(value,"یکی از گزینه‌های منو را انتخاب کنید یا وارد پنل مدیریت شوید."),webapp_button() or MENU)
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
