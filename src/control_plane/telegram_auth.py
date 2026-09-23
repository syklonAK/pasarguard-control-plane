from dataclasses import dataclass
import hashlib,hmac,json,time
from urllib.parse import parse_qsl
class TelegramAuthError(ValueError):pass
@dataclass(frozen=True)
class TelegramIdentity:
    user_id:int;first_name:str;last_name:str|None;username:str|None;auth_date:int

def verify_init_data(init_data:str,bot_token:str,max_age_seconds:int=3600,now:int|None=None)->TelegramIdentity:
    if not init_data or not bot_token:raise TelegramAuthError("Telegram authentication unavailable")
    values=dict(parse_qsl(init_data,keep_blank_values=True));received=values.pop("hash",None)
    if not received:raise TelegramAuthError("missing Telegram hash")
    check="\n".join(f"{k}={v}" for k,v in sorted(values.items()))
    secret=hmac.new(b"WebAppData",bot_token.encode(),hashlib.sha256).digest()
    expected=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,received):raise TelegramAuthError("invalid Telegram signature")
    auth_date=int(values.get("auth_date","0"));current=int(time.time()) if now is None else now
    if auth_date<=0 or current-auth_date>max_age_seconds or auth_date>current+30:raise TelegramAuthError("expired Telegram session")
    try:user=json.loads(values["user"]);user_id=int(user["id"])
    except (KeyError,ValueError,TypeError,json.JSONDecodeError) as exc:raise TelegramAuthError("invalid Telegram user") from exc
    return TelegramIdentity(user_id,user.get("first_name","") ,user.get("last_name"),user.get("username"),auth_date)
