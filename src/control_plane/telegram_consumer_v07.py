import asyncio
from . import telegram_consumer as base
def section_menu(chat_id):
 url=base.os.getenv("WEBAPP_URL","")
 if chat_id==base.root_id():rows=[[{"text":"📊 داشبورد","callback_data":"menu|dashboard"},{"text":"🖥 سرورها","callback_data":"menu|servers"}],[{"text":"👥 نمایندگان","callback_data":"menu|resellers"},{"text":"💰 درخواست‌های مالی","callback_data":"menu|finance"}],[{"text":"⚙️ مدیریت سیستم","callback_data":"menu|system"}]]
 else:rows=[[{"text":"🏠 حساب من","callback_data":"menu|dashboard"},{"text":"💳 کیف پول","callback_data":"menu|wallet"}],[{"text":"📈 مصرف","callback_data":"menu|usage"},{"text":"👥 زیرمجموعه‌ها","callback_data":"menu|resellers"}],[{"text":"📜 تراکنش‌ها","callback_data":"menu|transactions"},{"text":"🛟 پشتیبانی","callback_data":"menu|support"}]]
 if url:rows.append([{"text":"🌐 وب‌اپ پیشرفته" if chat_id==base.root_id() else "🌐 پنل کاربری","web_app":{"url":url}}])
 return {"inline_keyboard":rows}
async def show_section_menu(chat_id):
 title="منوی مدیریت کل" if chat_id==base.root_id() else "منوی نمایندگی";await base.send(chat_id,f"<b>{title}</b>\nبخش موردنظر را انتخاب کنید:",section_menu(chat_id))
async def main():
 original_message,original_callback=base.handle_message,base.handle_callback
 async def handle_message(redis,chat_id,value):
  value=(value or "").strip()
  if value in ("/menu","منو","🔙 منوی اصلی"):await show_section_menu(chat_id);return
  aliases={"/dashboard":"🏠 داشبورد مدیریت" if chat_id==base.root_id() else "🏠 حساب من","/servers":"🖥 مدیریت سرورها" if chat_id==base.root_id() else "🏠 حساب من","/support":"⚙️ مدیریت سیستم" if chat_id==base.root_id() else "🛟 پشتیبانی"};await original_message(redis,chat_id,aliases.get(value,value))
  if value.startswith("/start"):await show_section_menu(chat_id)
 async def handle_callback(chat_id,callback_id,data):
  if not data.startswith("menu|"):await original_callback(chat_id,callback_id,data);return
  await base.answer_callback(callback_id,"در حال بارگذاری…");section=data.split("|",1)[1]
  try:
   context=await asyncio.to_thread(base.context,chat_id)
   if not context:await base.ensure_linked(chat_id);return
   admin=chat_id==base.root_id()
   if section in ("dashboard","wallet","usage"):await base.send(chat_id,await asyncio.to_thread(base.dashboard,chat_id,admin),section_menu(chat_id))
   elif section=="servers" and admin:message,markup=await asyncio.to_thread(base.server_list,chat_id);await base.send(chat_id,message,markup)
   elif section=="resellers":await base.send(chat_id,await asyncio.to_thread(base.reseller_list,chat_id,admin),section_menu(chat_id))
   elif section=="finance" and admin:message,markup=await asyncio.to_thread(base.funding_requests,chat_id);await base.send(chat_id,message,markup or section_menu(chat_id))
   elif section=="transactions":await base.send(chat_id,await asyncio.to_thread(base.transactions,chat_id),section_menu(chat_id))
   elif section=="system" and admin:await base.send(chat_id,f"شناسه مدیر: <code>{chat_id}</code>\nعملیات روزمره از منو و عملیات پیشرفته از وب‌اپ در دسترس است.",section_menu(chat_id))
   elif section=="support":await base.send(chat_id,f"شناسه شما: <code>{chat_id}</code>\nبرای پیگیری این شناسه را به مدیر مجموعه بدهید.",section_menu(chat_id))
  except Exception as exc:print(f"submenu {data}: {exc}",flush=True);await base.send(chat_id,"عملیات انجام نشد. دوباره تلاش کنید.",section_menu(chat_id))
 base.handle_message,base.handle_callback=handle_message,handle_callback;await base.main()
if __name__=="__main__":asyncio.run(main())
