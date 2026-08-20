import hashlib
import secrets
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_ph = PasswordHasher()


def hash_password(plain: str) -> str:
    return _ph.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return _ph.verify(hashed, plain)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def new_raw_token() -> str:
    """A token shown to the user exactly once (in a link/QR)."""
    return secrets.token_urlsafe(32)


def hash_token(raw_token: str) -> str:
    """What we actually store — never the raw token."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def new_id() -> str:
    return secrets.token_hex(16)
