# PasarGuard B2B Control Plane

سامانه سازمانی فروش عمده، مدیریت نماینده و زیرشاخه برای PasarGuard با کیف پول، دفترکل مالی، محاسبه مصرف، Telegram Bot و Telegram WebApp.

> این پروژه ترافیک VPN را عبور نمی‌دهد. کاربران مستقیماً به نودهای PasarGuard/Xray متصل می‌شوند؛ Control Plane فقط مدیریت و حسابداری را انجام می‌دهد.

## امکانات

- ساختار نامحدود والد/فرزند برای نمایندگان
- تعرفه مستقل در هر رابطه والد و فرزند
- کیف پول، سقف اعتبار و دفترکل دوطرفه Append-only
- صورتحساب بر اساس `lifetime_used_traffic`
- تسویه آبشاری سود تمام سطوح
- اتصال هر نمایندگی به Admin پاسارگارد
- Billing Worker گروه‌بندی‌شده به تفکیک پنل
- Telegram Gateway و صف Redis
- WebApp انگلیسی و موبایل‌محور
- PostgreSQL، Redis، Nginx و HTTPS خودکار Caddy

## نصب تک‌کامندی

پیش‌نیاز: Ubuntu/Debian، حداقل 2 هسته CPU (پیشنهاد Production: چهار هسته یا بیشتر)، 8GB RAM، دامنه و Token ربات.

```bash
curl -fsSL https://raw.githubusercontent.com/syklonAK/pasarguard-control-plane/main/install.sh | sudo bash
```

بعد از نصب:

```bash
sudo bash /opt/pasarguard-control-plane/scripts/bootstrap.sh
```

## بروزرسانی با یک فایل

بعد از نصب اولیه، برای دریافت و اعمال نسخه جدید فقط اجرا کنید:

```bash
sudo bash /opt/pasarguard-control-plane/update.sh
```

Updater تنظیمات `.env` را حفظ می‌کند، نسخه جدید را دریافت می‌کند، سرویس‌ها را مجدد می‌سازد و Health Check انجام می‌دهد.

## رفع خطای CPU روی سرور دو هسته‌ای

اگر نصب قبلی هنگام ساخت Container متوقف شده، این دستورها را یک‌بار اجرا کنید:

```bash
cd /opt/pasarguard-control-plane
git pull --ff-only
sudo bash update.sh
```

## دستورات نگهداری

```bash
sudo bash /opt/pasarguard-control-plane/scripts/doctor.sh
sudo bash /opt/pasarguard-control-plane/scripts/backup.sh
sudo bash /opt/pasarguard-control-plane/update.sh
```

> پیش از ورود پول واقعی، تست بار، بازیابی Backup، Failover، MFA/RBAC، Approval عملیات حساس و Reconciliation مالی را آزمایش کنید.
