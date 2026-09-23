<div align="center">

# PasarGuard Control Plane

**کنترل‌پنل سازمانی فروش عمده و مدیریت نمایندگان برای پاسارگارد**

دفتر دو-طرفهٔ مصرفی، سلسله‌مراتب باز نمایندگان، دسترسی‌های رمزنگاری‌شدهٔ پنل، شش نقش سمت‌سروری،
ربات تلگرام و وب‌اپ تلگرام.

[![CI](https://github.com/syklonAK/pasarguard-control-plane/actions/workflows/quality.yml/badge.svg)](https://github.com/syklonAK/pasarguard-control-plane/actions/workflows/quality.yml)
![Python](https://img.shields.io/badge/Python-3.12%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.11x-009688)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-17-336791)
![Redis](https://img.shields.io/badge/Redis-7-dc382c)
![UI](https://img.shields.io/badge/وب%E2%80%8Cاپ-فارسی%20RTL-6c5ce7)

</div>

---

این پروژه یک **لایهٔ کنترل کسب‌وکار** است، نه پروکسی و نه تونل: هیچ ترافیک اشتراکی از آن عبور
نمی‌کند و هرگز در مسیر دادهٔ مشترک قرار نمی‌گیرد. برای ~۱۰٬۰۰۰ نماینده و ~۱٬۰۰۰ اپراتور همزمان
اندازه‌گذاری شده است (جزئیات: [docs/CAPACITY-10K.md](docs/CAPACITY-10K.md)).

## چه کاری انجام می‌دهد

| حوزه | رفتار پیاده‌شده |
| --- | --- |
| مالی | دفتر دو-طرفه؛ هر تغییر موجودی فقط از مسیر `Transaction`، با تریگر `plpgsql` در دیتابیس که نوشتنِ مستقیم موجودی را رد می‌کند |
| اعتبار | سقف اعتبار، درخواست تأمین موجودی با تأیید/رد، و تسویهٔ آبشاری به همهٔ والدها با سود هر سطح |
| اصلاح حساب | فقط با تأیید دوم نفره (درخواست‌کننده ≠ تأییدکننده) و ثبت در رسیدگی |
| ساختار | درخت باز نمایندگان با جدول closure، عمق سقف‌دار (`MAX_TREE_DEPTH`) و contracts جدا برای هر سطح |
| پاسارگارد | رجیستری چند-پنلی، تست اتصال پیش از ذخیره، کلید API و رمز مالک با Fernet (`enc://…`)، عملیات نود/مشترک/ادمین |
| مصرف | checkpoint های دوره‌ای، `usage_coefficient` برای هر پنل، تشخیص reset و regression بدون صورتحساب تکراری |
| رویدادها | Outbox با تحویل حداقل-یک‌بار، backoff و وضعیت dead-letter؛ hold روی مصرف تا تسویه |
| رابط | ربات تلگرام فارسی + وب‌اپ RTL با قابلیت‌محوری (هیچ تصمیم دسترسی در مرورگر) |
| عملیات | healthcheck، metrics، لاگ ساخت‌یافته، backup/restore/rotation، doctor، نصب و به‌روزرسانی با یک دستور |

## معماری

```text
Telegram ──webhook──▶ telegram-gateway ──▶ Redis Streams ──▶ telegram-consumer (ربات)
                                     │
مرورگر/وب‌اپ ──HTTPS──▶ gateway ──▶ api (FastAPI) ◀──── Caddy/nginx ──▶ webapp/ (RTL)
                                     │           │
                                     │           ├─▶ PostgreSQL (دفتر، موجودی کش‌شده، outbox)
                                     │           └─▶ PasarGuard panel API
                       billing-worker ─┴─ outbox-worker
```

| سرویس (docker-compose.enterprise.yml) | نقش |
| --- | --- |
| `gateway` / `caddy` | TLS، هدرهای امنیتی، HSTS، CSP `frame-ancestors` |
| `api` | مسیرهای `/v1/…` (ماشین، `X-Control-Key`) و `/v1/webapp/…` (انسان، امضای `initData`) |
| `postgres` | دفتر، قفل ردیف، view تشخیص ناعدی `ledger_imbalance` |
| `redis` | محدودساز نرخ، state فرم، dedupe رویداد تلگرام، صف استریم |
| `billing-worker` | polls مصرف، checkpoint، تسویهٔ آبشاری، hold |
| `outbox-worker` | تحویل رویدادها با backoff |
| `telegram-gateway` / `telegram-consumer` | دریافت webhook و کارگر ربات با consumer group |

مسیر کد: `src/control_plane/rbac.py` تنها مرجع واقعیت نقش است؛ هم کیبورد ربات و هم منوی وب‌اپ از
`/v1/webapp/capabilities` ساخته می‌شوند و هر route همان ماتریس را سمت سرور دوباره بررسی می‌کند.

## نصب سریع

پیش‌نیاز: Ubuntu 22.04/24.04 یا Debian 12، دامنه‌ای که به سرور اشاره می‌کند، پورت‌های 80/443 آزاد، و
توکن ربات تلگرام.

```bash
curl -fsSL https://raw.githubusercontent.com/syklonAK/pasarguard-control-plane/main/install.sh | sudo bash
```

نصب‌کننده داکر را در صورت نبود نصب می‌کند، `.env` با مجوز `600` می‌نویسد، شناسهٔ تلگرام مدیر کل
(`ROOT_TELEGRAM_ID`) و توکن ربات را می‌پرسد، webhook را ثبت می‌کند و تایمر backup شبانه
(`pasarguard-backup.timer`) را فعال می‌سازد. اجرای مجدد، رازهای موجود را بازنویسی نمی‌کند.

### راه‌اندازی اولیه (وب‌اپ، نه ترمینال)

```bash
sudo grep INITIAL_SETUP_TOKEN /root/pasarguard-control-plane-credentials.txt
```

ربات را باز کنید، «وب‌اپ مدیریت» را بزنید و کسب‌وکار ریشه را با آن توکن بسازید. مسیر bootstrap تنها
برای `ROOT_TELEGRAM_ID` و فقط یک‌بار باز است. سپس از **سرورها ← افزودن سرور** پنل‌های پاسارگارد را
ثبت کنید؛ کلید API و رمز مالک پیش از ذخیره روی پنل تست می‌شوند، رمزنگاری می‌شوند و از هیچ endpoint
خواندنی برنمی‌گردند. ثبت سرور هرگز از طریق سؤالات ترمینال انجام نمی‌شود.

## نقش‌ها

| نقش | دسترسی |
| --- | --- |
| مدیر کل سیستم (`system_admin`) | همه‌چیز، از جمله تأیید اصلاح حساب، ضریب صورتحساب و خاموش‌کردن اعتبارسنجی TLS؛ از `ROOT_TELEGRAM_ID` مشتق می‌شود و قابل تخصیص نیست |
| مدیر نمایندگی (`reseller_admin`) | مشاهده، درخواست و تأیید مالی، سرورها، نودها، مشترکان، سقف پنل، مدیریت نمایندگان |
| اپراتور (`operator`) | داشبورد/رسیدگی/خروجی، ثبت سرور، کنترل نود و مشترک، تغییر سقف پنل |
| مدیر مالی (`finance`) | مشاهدهٔ مالی و رسیدگی، درخواست افزایش اعتبار و اصلاح حساب، تأیید درخواست مالی |
| پشتیبان (`support`) | داشبورد، مالی (فقط مشاهده)، تیکت و پاسخ، مدیریت مشترکان |
| مشاهده‌گر (`viewer`) | داشبورد، مشاهدهٔ مالی، خروجی گزارش |

نقش از ردیف عضویت **فعال** سمت سرور خوانده می‌شود؛ نقش ناشناخته بدون هیچ امتیازی (`viewer` بی‌دسترسی
خاص) در نظر گرفته می‌شود.

## عملیات

```bash
cd /opt/pasarguard-control-plane

bash scripts/doctor.sh                     # سلامت سرویس‌ها، MIME، باز/بسته بودن metrics، drift دفتر، lag استریم
bash scripts/backup.sh                     # dump تأییدشده + چرخش نسخه‌ها (BACKUP_RETENTION)
bash scripts/restore.sh backups/control-<STAMP>.sql.gz          # پیش‌فرض روی دیتابیس scratch
bash scripts/restore.sh <file> --into control --force           # جایگزینی زنده، پس از backup ایمنی
sudo bash update.sh                        # pull با fast-forward، migration idempotent، rebuild، webhook
sudo bash uninstall.sh                     # داده و volume ها می‌مانند؛ پاک کردن فقط با --purge-data
```

`update.sh` هرگز `.env` را بازنویسی نمی‌کند، volume ها را حذف نمی‌کند و اگر migration شکست بخورد
راه‌اندازی نمی‌کند. داکر در حذف نصب هرگز برداشته نمی‌شود.

## پیکربندی

تمام کلیدها در [`.env.example`](.env.example) مستندند. آنچه رفتار را عوض می‌کند نه credential را:

| کلید | پیش‌فرض | کاربرد |
| --- | --- | --- |
| `METRICS_TOKEN` | خالی | با تنظیم‌نشدن، `/metrics` بسته می‌ماند |
| `RATE_LIMIT_PER_MINUTE` / `RATE_LIMIT_WRITE_PER_MINUTE` | — | محدودساز نرخ نوشتن‌های ناشناس و webhook |
| `TRUST_PROXY_HEADERS` | `false` | فقط پشت پروکسی که `X-Forwarded-For` را بازنویسی می‌کند |
| `EXPOSE_DOCS` | `false` | فعال‌سازی `/docs` |
| `MAX_TREE_DEPTH` | `5` | سقف عمق درخت نمایندگان |
| `APPROVAL_TTL_SECONDS` | `604800` | انقضای درخواست‌های تأیید نشده |
| `HOLD_LIMIT_BYTES` | `1` | آستانهٔ hold مصرف قبل از تسویه |
| `BACKUP_RETENTION` | `30` | تعداد نسخه‌های نگه‌داشته‌شده |

## ساختار مخزن

```text
src/control_plane/
  app.py                هستهٔ دامنه: مدل‌ها، دفتر، سلسله‌مراتب، رازها، onboarding
  api.py                API ماشین (/v1/…) با نگهبان X-Control-Key
  web_api.py            API انسان (/v1/webapp/…) با نگهبان امضای initData
  rbac.py               ماتریس دسترسی؛ تنها مرجع واقعیت نقش
  main.py               ورودی ASGI: میدل‌ورها، هدرهای امنیتی، /health، /metrics
  observability.py      لاگ JSON ساخت‌یافته، محدودساز نرخ، متن Prometheus
  telegram_gateway.py   دریافت webhook: بررسی HMAC، dedupe update_id، enqueue در Redis Stream
  telegram_consumer.py  کارگر منوی ربات (consumer group)، پاسخ‌های فارسی
  pasarguard.py         کلاینت نازک HTTP پاسارگارد (تک‌محل مسیرهای پنل)
  outbox.py             تحویل حداقل-یک‌بار رویداد با backoff و dead-letter
  worker.py             polls صورتحساب: checkpoint، تسویهٔ آبشاری، hold
  migrate.py            runner مهاجرت با advisory lock و idempotent
webapp/                 وب‌اپ فارسی RTL تلگرام (تک‌صفحه، قابلیت‌محور)
migrations/             SQL ترتیب‌دار، روی دیتابیس خالی و موجود یکسان
scripts/                backup، restore، doctor، release-check، configure-telegram
deploy/                 پیکربندی nginx و Caddy
load/k6.js              پروفایل بار ۱۰k کاربر / ۱k همزمان
docs/                   معماری، onboarding، ظرفیت
```

## تست و بازبینی

```bash
pip install -e .[test] && pytest        # ۱۰۷ تست روی PostgreSQL واقعی، شامل تریگرهای دفتر
bash scripts/release-check.sh            # سینتکس shell/Python/JS، YAML کامپوز، اسکن الگوی راز
k6 run load/k6.js                        # پروفایل بار مستند
```

در GitHub Actions دو job اجرا می‌شود: `static` (release-check) و `test` روی سرویس
`postgres:17-alpine`، تا migration و تریگرها روی همان رفتار SQL production سنجیده شوند.

## مستندات

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — ماژول‌ها، ثابت‌های پولی، مصرف و RBAC
- [docs/ONBOARDING.md](docs/ONBOARDING.md) — راه‌اندازی ۹ مرحله‌ای از مسیر وب‌اپ
- [docs/CAPACITY-10K.md](docs/CAPACITY-10K.md) — فرض‌های ظرفیت و پروفایل بار
- [SECURITY.md](SECURITY.md) — رازها، هویت، ورودی‌ها و عملیات امنیتی

## محدودیت‌های شناخته‌شده

- آدرس پنل فقط از نظر `https`، قالب hostname و نبود credentials بررسی می‌شود؛ blocklist برای
  دامنه‌های داخلی و IP خصوصی (دفاع SSRF) پیاده نشده است.
- اجرای واقعی restore drill و پروفایل k6 روی زیرساخت production بر عهدهٔ اپراتور است.
- برای پذیرش پول واقعی: تطبیق با پنل staging، تکمیل گردش تأیید دوم نفره و یک تمرین بازیابی انجام شود.
