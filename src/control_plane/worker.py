import os,time
from collections import defaultdict
from datetime import datetime,timezone
from sqlalchemy import select
from .app import BillingHold,Binding,Panel,SessionLocal,UsageIn,observe,record_hold
from .pasarguard import Client
from .secrets import resolve_secret

def secret(ref): return resolve_secret(ref)

def poll():
 with SessionLocal() as s:
  grouped=defaultdict(list)
  for b in s.scalars(select(Binding).where(Binding.status=="active")).all():grouped[b.panel_id].append(b)
  for panel_id,bindings in grouped.items():
   panel=s.get(Panel,panel_id)
   if not panel or panel.status!="active":continue
   try:
    client=Client(panel.base_url,secret(panel.api_key_ref),secret(panel.owner_user_ref),secret(panel.owner_pass_ref),panel.verify_tls)
    admins={x.get("id"):x for x in client.admins().get("admins",[])}
   except Exception as exc:print(f"panel {panel_id}: {exc}",flush=True);continue
   for b in bindings:
    admin=admins.get(b.pg_admin_id)
    if not admin or admin.get("lifetime_used_traffic") is None:continue
    try:observe(s,UsageIn(binding_id=b.id,lifetime_bytes=int(admin["lifetime_used_traffic"]),observed_at=datetime.now(timezone.utc)));s.commit()
    except BillingHold as exc:s.rollback();record_hold(s,exc.binding_id,exc.organization_id);s.commit()
    except Exception as exc:s.rollback();print(f"binding {b.id}: {exc}",flush=True)
def main():
 while True:poll();time.sleep(int(os.getenv("USAGE_POLL_SECONDS","30")))
if __name__=="__main__":main()
