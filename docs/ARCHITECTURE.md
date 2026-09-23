# Architecture
PasarGuard منبع وضعیت فنی و مصرف است؛ Control Plane منبع پول، قیمت، مالکیت و سلسله‌مراتب.

```text
control-api ─┬─ PostgreSQL (hierarchy + ledger + contracts)
             ├─ worker-billing ─ PasarGuard API
             └─ telegram-gateway
```
هر نماینده یک Organization و یک PasarGuard Admin binding دارد. درخت در Control Plane نگهداری می‌شود و PasarGuard نیازی به پشتیبانی native از زیرشاخه ندارد.
