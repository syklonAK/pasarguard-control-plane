from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
GIB=1024**3

def price_usage(delta_bytes:int, rate_irr:int, coefficient:Decimal|int|str=1)->int:
    if delta_bytes<0 or rate_irr<0: raise ValueError("negative usage/rate")
    factor=Decimal(str(coefficient))
    if factor<0: raise ValueError("negative usage coefficient")
    return int((Decimal(delta_bytes)*Decimal(rate_irr)*factor/Decimal(GIB)).quantize(Decimal("1"),rounding=ROUND_HALF_UP))

@dataclass(frozen=True)
class Settlement:
    child_id:str; parent_id:str; delta_bytes:int; rate_irr:int; amount_irr:int

def cascade(delta_bytes:int, edges:list[tuple[str,str,int]], coefficient:Decimal|int|str=1)->list[Settlement]:
    return [Settlement(c,p,delta_bytes,r,price_usage(delta_bytes,r,coefficient)) for c,p,r in edges if delta_bytes>0]
