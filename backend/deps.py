from fastapi import Header, HTTPException
from auth_service import get_session_user


def _extract_token(authorization: str) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return authorization[len("Bearer "):].strip()


def require_user(authorization: str = Header(default="")):
    token = _extract_token(authorization)
    user = get_session_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    return user


def require_super_admin(authorization: str = Header(default="")):
    user = require_user(authorization)
    if user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="Super Admin only")
    return user


def require_admin_or_above(authorization: str = Header(default="")):
    user = require_user(authorization)
    if user["role"] not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin only")
    return user


def require_tenant(authorization: str = Header(default="")):
    user = require_user(authorization)
    if user["role"] != "tenant":
        raise HTTPException(status_code=403, detail="Tenant only")
    if not user["unit_id"]:
        raise HTTPException(status_code=403, detail="Tenant account has no unit assigned")
    return user
