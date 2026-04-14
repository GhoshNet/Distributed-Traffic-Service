"""
JWT authentication utilities shared across all services.
"""

import os
from datetime import datetime, timedelta
from typing import Optional
import jwt
from fastapi import HTTPException, Security, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

_DEFAULT_SECRET = "super-secret-jwt-key-change-in-production"
JWT_SECRET = os.getenv("JWT_SECRET", _DEFAULT_SECRET)
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24

if JWT_SECRET == _DEFAULT_SECRET:
    import logging as _log
    _log.getLogger(__name__).warning(
        "JWT_SECRET is using the default value. "
        "Set the JWT_SECRET environment variable before deploying to production."
    )

security = HTTPBearer()


def create_access_token(user_id: str, email: str, license_number: str, role: str = "DRIVER", full_name: str = "") -> tuple[str, int]:
    """Create a JWT access token. Returns (token, expires_in_seconds)."""
    expires_delta = timedelta(hours=JWT_EXPIRATION_HOURS)
    expire = datetime.utcnow() + expires_delta

    payload = {
        "sub": user_id,
        "email": email,
        "license": license_number,
        "role": role,
        "full_name": full_name,
        "exp": expire,
        "iat": datetime.utcnow(),
    }

    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return token, int(expires_delta.total_seconds())


def decode_token(token: str) -> dict:
    """Decode and validate a JWT token."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> dict:
    """FastAPI dependency that extracts and validates the current user from JWT."""
    payload = decode_token(credentials.credentials)
    return {
        "user_id": payload["sub"],
        "email": payload["email"],
        "license": payload.get("license"),
        "role": payload.get("role", "DRIVER"),
        "full_name": payload.get("full_name", ""),
    }


def require_role(required_role: str):
    """Dependency generator that checks if the JWT has the required role."""
    async def role_checker(current_user: dict = Depends(get_current_user)) -> dict:
        if current_user.get("role") != required_role and current_user.get("role") != "ADMIN":
            raise HTTPException(
                status_code=403,
                detail=f"Operation requires {required_role} role",
            )
        return current_user
    return role_checker


class OptionalAuth:
    """Dependency that makes auth optional (for public endpoints)."""

    async def __call__(
        self,
        credentials: Optional[HTTPAuthorizationCredentials] = Security(
            HTTPBearer(auto_error=False)
        ),
    ) -> Optional[dict]:
        if credentials is None:
            return None
        try:
            return decode_token(credentials.credentials)
        except HTTPException:
            return None
