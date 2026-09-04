"""
Authentication utilities
------------------------
- Password hashing & verification (bcrypt)
- JWT creation & decoding (python‑jose)
- OTP generation (secrets module)
"""

import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from app.config import JWT_ALGORITHM, JWT_EXPIRATION_HOURS, JWT_SECRET


def hash_password(password: str) -> str:
    """Return a bcrypt hash of the plain‑text password."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Check a plain‑text password against its bcrypt hash."""
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))


def create_access_token(user_id: int, role: str) -> str:
    to_encode = {
        "sub": str(user_id),
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION_HOURS),
    }
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])


def generate_otp(length: int = 6) -> str:
    """Generate a cryptographically‑secure numeric OTP string."""
    return "".join(secrets.choice("0123456789") for _ in range(length))