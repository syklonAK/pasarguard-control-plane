# Capacity plan: 10,000 users / 1,000 concurrent online

## Important boundary
VPN subscription traffic and 1,000 concurrent sessions must stay on PasarGuard/Xray nodes. Control Plane is not a proxy and must never sit in the data path. It handles reseller commands, billing, webhooks and aggregate usage only.

## Initial production topology
- 2–3 Control API replicas, 4 Uvicorn workers each
- 2 Telegram consumers; webhook gateway returns after Redis enqueue
- PostgreSQL primary: 4 vCPU / 8–16 GB RAM / NVMe; daily backup + PITR
- Redis: 1–2 GB with AOF, no-eviction
- Billing polls admin aggregates, not all 10,000 users
- Separate owner-operation worker and network allowlist for PasarGuard

## SLO targets
- API availability: 99.9%
- read p95 < 300 ms, write p95 < 600 ms
- Telegram webhook ACK p95 < 100 ms
- billing freshness < 60 s
- RPO <= 5 min, RTO <= 30 min

## Release gate
Do not call the system production-ready until: k6 profile passes, PostgreSQL failover is tested, restore drill passes, PasarGuard staging reconciliation has zero unexplained deltas, and 24-hour soak test completes without ledger imbalance.
