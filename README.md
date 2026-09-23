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
- WebApp فارسی و موبایل‌محور
- PostgreSQL، Redis، Nginx و HTTPS خودکار Caddy

## نصب تک‌کامندی

### پیش‌نیازها

- Ubuntu 22.04/24.04 یا Debian 12
- حداقل 4 CPU و 8GB RAM
- دامنه متصل به IP سرور
- باز بودن پورت‌های 80 و 443
- Token ربات تلگرام
- آدرس HTTPS و API Key پاسارگارد

```bash
curl -fsSL https://raw.githubusercontent.com/syklonAK/pasarguard-control-plane/main/install.sh | sudo bash
```

بعد از نصب:

```bash
sudo bash /opt/pasarguard-control-plane/scripts/bootstrap.sh
```

آدرس WebApp در BotFather:

```text
https://YOUR-DOMAIN/app/
```

## نگهداری

```bash
sudo bash /opt/pasarguard-control-plane/scripts/doctor.sh
sudo bash /opt/pasarguard-control-plane/scripts/backup.sh
sudo bash /opt/pasarguard-control-plane/scripts/update.sh
```

## ظرفیت و هشدار Production

معماری برای 10,000 کاربر و 1,000 اتصال هم‌زمان طراحی شده است، اما تأیید نهایی ظرفیت به تست بار روی Staging مشابه Production نیاز دارد. قبل از ورود پول واقعی، بازیابی Backup، Failover، MFA/RBAC، Approval عملیات حساس و Reconciliation مالی را آزمایش کنید.
