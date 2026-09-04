"""
Authentication & User Management Router

Endpoints:
    POST /register          – create a new farmer/vendor account
    POST /verify-phone      – activate account with OTP
    POST /login             – authenticate and receive JWT
    POST /forgot-password   – request a password-reset OTP
    POST /forgot-password/resend – resend password-reset OTP
    POST /reset-password    – set a new password using the OTP
    GET  /me                – view own profile
    PUT  /me                – update own profile
    PUT  /me/password       – change password while logged in
    POST /me/deactivate     – deactivate own account
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.database import get_session
from app.models import User, VerificationCode, PasswordResetToken, NotificationType, get_utc_now_naive
from app.services.auth import (
    create_access_token,
    decode_access_token,
    generate_otp,
    hash_password,
    verify_password,
)
from app.services.sms import send_sms
from app.services.notification_service import create_notification
from app.services.validation import validate_ghana_phone

router = APIRouter()

# ---------------------------------------------------------------------------
# OAuth2 scheme for token extraction (JWT from Authorization header)
# ---------------------------------------------------------------------------
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

# ---------------------------------------------------------------------------
# JWT Dependency – extracts current user from cookie or Bearer token
# ---------------------------------------------------------------------------
def get_current_user(
    request: Request,
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_session),
) -> User:
    """Decode JWT and return the authenticated user.
    Checks the 'access_token' cookie first, then falls back to the
    Authorization header (Bearer token).
    """
    access_token = request.cookies.get("access_token")
    if not access_token:
        if not token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
        access_token = token

    try:
        payload = decode_access_token(access_token)
        user_id = int(payload["sub"])
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user

# ---------------------------------------------------------------------------
# Reusable password policy validator (kept local)
# ---------------------------------------------------------------------------
def _validate_password_policy(v: str) -> str:
    if len(v) < 8:
        raise ValueError("Password must be at least 8 characters long.")
    if len(v) > 72:
        raise ValueError("Password must be at most 72 characters long.")
    if not any(c.islower() for c in v):
        raise ValueError("Password must include at least one lowercase letter.")
    if not any(c.isupper() for c in v):
        raise ValueError("Password must include at least one uppercase letter.")
    if not any(c.isdigit() for c in v):
        raise ValueError("Password must include at least one digit.")
    if not any(not c.isalnum() for c in v):
        raise ValueError("Password must include at least one special character.")
    return v

# ---------------------------------------------------------------------------
# Request / Response Schemas
# ---------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    name: str
    phone: str
    town: str
    region: str
    district: Optional[str] = None
    password: str = Field(min_length=8, max_length=72)
    role: str = Field(pattern="^(farmer|vendor)$")
    email: Optional[EmailStr] = None
    accepted_terms: bool   # <-- required, no default

    @field_validator('phone')
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return validate_ghana_phone(v)

    @field_validator('email', mode='before')
    @classmethod
    def normalise_email(cls, v):
        if isinstance(v, str):
            v = v.strip()
            if v == "":
                return None
        return v

    @field_validator('password')
    @classmethod
    def check_password_strength(cls, v: str) -> str:
        return _validate_password_policy(v)

    @field_validator('accepted_terms')
    @classmethod
    def check_consent(cls, v: bool) -> bool:
        if not v:
            raise ValueError("You must accept the Terms of Service and Privacy Policy to register.")
        return v


class VerifyPhoneRequest(BaseModel):
    phone: str
    code: str

    @field_validator('phone')
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return validate_ghana_phone(v)


class LoginRequest(BaseModel):
    phone: str
    password: str

    @field_validator('phone')
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return validate_ghana_phone(v)


class ForgotPasswordRequest(BaseModel):
    phone: str

    @field_validator('phone')
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return validate_ghana_phone(v)


class ResetPasswordRequest(BaseModel):
    phone: str
    code: str
    new_password: str = Field(min_length=8, max_length=72)

    @field_validator('phone')
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return validate_ghana_phone(v)

    @field_validator('new_password')
    @classmethod
    def check_password_strength(cls, v: str) -> str:
        return _validate_password_policy(v)


class UpdateProfileRequest(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    town: Optional[str] = None
    region: Optional[str] = None
    district: Optional[str] = None
    email: Optional[EmailStr] = None

    @field_validator('phone')
    @classmethod
    def validate_phone_if_provided(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return validate_ghana_phone(v)
        return v

    @field_validator('email', mode='before')
    @classmethod
    def normalise_email_if_provided(cls, v):
        if isinstance(v, str):
            v = v.strip()
            if v == "":
                return None
        return v


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=72)

    @field_validator('new_password')
    @classmethod
    def check_password_strength(cls, v: str) -> str:
        return _validate_password_policy(v)


# ---------------------------------------------------------------------------
# POST /register
# ---------------------------------------------------------------------------
@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, db: Session = Depends(get_session)):
    """Create a new user (unverified) and send an SMS OTP."""
    phone = body.phone  # already normalised by validator
    email = body.email  # already normalised by validator (None if empty)

    # Check duplicate phone
    existing_phone = db.exec(select(User).where(User.phone == phone)).first()
    if existing_phone:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Phone already registered")

    # Check duplicate email if provided
    if email:
        existing_email = db.exec(select(User).where(User.email == email)).first()
        if existing_email:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = User(
        name=body.name.strip(),
        phone=phone,
        password_hash=hash_password(body.password),
        role=body.role,
        town=body.town,
        region=body.region,
        district=body.district,
        email=email,
        is_active=False,
        accepted_terms_at=get_utc_now_naive(),  # store consent timestamp
    )
    db.add(user)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this phone or email already exists")

    db.refresh(user)

    code = generate_otp()
    expires = get_utc_now_naive() + timedelta(minutes=10)
    vc = VerificationCode(user_id=user.id, code=code, purpose="signup", expires_at=expires)
    db.add(vc)
    db.commit()

    send_sms(
        phone=phone,
        message=f"Your ASPEN verification code is: {code}",
        user_id=user.id,
        db_session=db,
    )

    return {
        "message": "Registration successful. A verification code has been sent to your phone.",
        "sms_status": "sent",
    }


# ---------------------------------------------------------------------------
# POST /verify-phone
# ---------------------------------------------------------------------------
@router.post("/verify-phone")
def verify_phone(body: VerifyPhoneRequest, db: Session = Depends(get_session)):
    """Activate a user account using the OTP sent to their phone."""
    phone = body.phone
    user = db.exec(select(User).where(User.phone == phone)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    vc = db.exec(
        select(VerificationCode)
        .where(
            VerificationCode.user_id == user.id,
            VerificationCode.code == body.code,
            VerificationCode.used == False,
            VerificationCode.purpose == "signup",
        )
        .order_by(VerificationCode.created_at.desc())
    ).first()

    if not vc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired code")

    if vc.expires_at < get_utc_now_naive():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Code has expired")

    vc.used = True
    user.is_active = True
    db.add(vc)
    db.add(user)
    db.commit()

    return {"message": "Phone verified successfully. You can now log in."}


# ---------------------------------------------------------------------------
# POST /login
# ---------------------------------------------------------------------------
@router.post("/login")
def login(body: LoginRequest, db: Session = Depends(get_session)):
    """Authenticate a user and return a JWT."""
    phone = body.phone
    user = db.exec(select(User).where(User.phone == phone)).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid phone or password")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is deactivated. Please contact support.")

    token = create_access_token(user.id, user.role)
    return {
        "access_token": token,
        "token_type": "bearer",
        "role": user.role,
        "name": user.name,
    }


# ---------------------------------------------------------------------------
# POST /forgot-password
# ---------------------------------------------------------------------------
@router.post("/forgot-password")
def forgot_password(body: ForgotPasswordRequest, db: Session = Depends(get_session)):
    """Send a password‑reset OTP to the registered phone number."""
    phone = body.phone
    user = db.exec(select(User).where(User.phone == phone)).first()

    if not user:
        return {"message": "If that phone is registered, a reset code has been sent."}

    now = get_utc_now_naive()

    recent_token = db.exec(
        select(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used == False,
        )
        .order_by(PasswordResetToken.created_at.desc())
    ).first()

    if recent_token:
        age_seconds = (now - recent_token.created_at).total_seconds()
        if age_seconds < 60:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Please wait before requesting another code.",
            )

    old_tokens = db.exec(
        select(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used == False,
        )
    ).all()

    for old_token in old_tokens:
        old_token.used = True
        db.add(old_token)

    code = generate_otp()
    expires = now + timedelta(minutes=10)
    token = PasswordResetToken(user_id=user.id, token=code, expires_at=expires)
    db.add(token)
    db.commit()

    send_sms(
        phone=phone,
        message=f"Your ASPEN password-reset code is: {code}",
        user_id=user.id,
        db_session=db,
    )

    return {"message": "If that phone is registered, a reset code has been sent."}


# ---------------------------------------------------------------------------
# POST /forgot-password/resend
# ---------------------------------------------------------------------------
@router.post("/forgot-password/resend")
def forgot_password_resend(body: ForgotPasswordRequest, db: Session = Depends(get_session)):
    """Resend a password-reset OTP with rate limiting and old code invalidation."""
    phone = body.phone
    user = db.exec(select(User).where(User.phone == phone)).first()
    if not user:
        return {"message": "If that phone is registered, a reset code has been sent."}

    now = get_utc_now_naive()

    recent_token = db.exec(
        select(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used == False,
        )
        .order_by(PasswordResetToken.created_at.desc())
    ).first()

    if recent_token:
        age_seconds = (now - recent_token.created_at).total_seconds()
        if age_seconds < 60:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Please wait before requesting another code.",
            )

    old_tokens = db.exec(
        select(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used == False,
        )
    ).all()

    for old_token in old_tokens:
        old_token.used = True
        db.add(old_token)

    code = generate_otp()
    expires = now + timedelta(minutes=10)
    new_token = PasswordResetToken(user_id=user.id, token=code, expires_at=expires)
    db.add(new_token)
    db.commit()

    send_sms(
        phone=phone,
        message=f"Your ASPEN password-reset code is: {code}",
        user_id=user.id,
        db_session=db,
    )

    return {"message": "A new password reset code has been sent."}


# ---------------------------------------------------------------------------
# POST /reset-password
# ---------------------------------------------------------------------------
@router.post("/reset-password")
def reset_password(body: ResetPasswordRequest, db: Session = Depends(get_session)):
    """Set a new password using the OTP received via SMS."""
    phone = body.phone
    user = db.exec(select(User).where(User.phone == phone)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    reset_token = db.exec(
        select(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.token == body.code,
            PasswordResetToken.used == False,
        )
        .order_by(PasswordResetToken.created_at.desc())
    ).first()

    if not reset_token or reset_token.expires_at < get_utc_now_naive():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired code")

    user.password_hash = hash_password(body.new_password)
    reset_token.used = True
    db.add(user)
    db.add(reset_token)
    db.commit()

    return {"message": "Password reset successfully. You can now log in."}


# ---------------------------------------------------------------------------
# GET /me – view own profile (requires authentication)
# ---------------------------------------------------------------------------
@router.get("/me")
def view_profile(current_user: User = Depends(get_current_user)):
    return {
        "name": current_user.name,
        "phone": current_user.phone,
        "role": current_user.role,
        "town": current_user.town,
        "region": current_user.region,
        "district": current_user.district,
        "email": current_user.email,
        "is_active": current_user.is_active,
    }


# ---------------------------------------------------------------------------
# PUT /me – update own profile (requires authentication)
# ---------------------------------------------------------------------------
@router.put("/me")
def update_profile(
    body: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    if body.name is not None:
        current_user.name = body.name.strip()

    if body.phone is not None:
        cleaned_phone = body.phone  # already normalised
        if cleaned_phone != current_user.phone:
            exists = db.exec(select(User).where(User.phone == cleaned_phone)).first()
            if exists:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Phone already taken")
        current_user.phone = cleaned_phone

    if body.town is not None:
        current_user.town = body.town

    if body.region is not None:
        current_user.region = body.region

    if body.district is not None:
        current_user.district = body.district

    if body.email is not None:
        # Already normalised (None if empty)
        new_email = body.email
        if new_email != current_user.email:
            if new_email:
                email_exists = db.exec(select(User).where(User.email == new_email)).first()
                if email_exists:
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already taken")
            current_user.email = new_email

    db.add(current_user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Phone or email already taken")

    return {"message": "Profile updated"}


# ---------------------------------------------------------------------------
# PUT /me/password – change password while logged in
# ---------------------------------------------------------------------------
@router.put("/me/password")
def change_password(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    if not verify_password(body.current_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")

    current_user.password_hash = hash_password(body.new_password)
    db.add(current_user)

    create_notification(
        db,
        current_user.id,
        "Security alert: Your password was changed successfully. If this was not you, contact support.",
        NotificationType.WARNING,
    )

    db.commit()

    send_sms(
        phone=current_user.phone,
        message="[ASPEN] Security alert: Your password was changed successfully. If this was not you, contact support.",
        user_id=current_user.id,
        db_session=db,
    )

    return {"message": "Password changed successfully"}


# ---------------------------------------------------------------------------
# POST /me/deactivate – deactivate own account
# ---------------------------------------------------------------------------
@router.post("/me/deactivate")
def deactivate_account(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    phone = current_user.phone

    current_user.is_active = False
    db.add(current_user)

    create_notification(
        db,
        current_user.id,
        "Your ASPEN account has been deactivated. Contact support if this was a mistake.",
        NotificationType.INFO,
    )

    db.commit()

    send_sms(
        phone=phone,
        message="Your ASPEN account has been deactivated. Contact support if this was a mistake.",
        user_id=current_user.id,
        db_session=db,
    )

    return {"message": "Account deactivated. Contact support to reactivate."}