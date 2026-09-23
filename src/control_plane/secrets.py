import base64
import hashlib
import os
from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc://"

def _fernet() -> Fernet:
    master = os.getenv("APP_ENCRYPTION_KEY", "")
    if len(master) < 32: raise RuntimeError("APP_ENCRYPTION_KEY must contain at least 32 characters")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(master.encode()).digest()))

def encrypt_secret(value: str | None) -> str | None:
    return PREFIX + _fernet().encrypt(value.encode()).decode() if value else None

def resolve_secret(reference: str | None) -> str | None:
    if not reference: return None
    if reference.startswith("env://"):
        value=os.getenv(reference[6:],"")
        if not value: raise RuntimeError(f"missing secret environment variable: {reference[6:]}")
        return value
    if reference.startswith(PREFIX):
        try: return _fernet().decrypt(reference[len(PREFIX):].encode()).decode()
        except InvalidToken as exc: raise RuntimeError("encrypted secret cannot be decrypted") from exc
    raise RuntimeError("unsupported secret reference")
