"""
Web Router — serves Jinja2 templates and handles browser‑based auth flows.

Flash messages are passed to every template via the `template_context` helper.
Admin pages (/admin/*) are served by the admin router, not here.
"""

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select, func
from sqlalchemy.orm import selectinload
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from email_validator import validate_email, EmailNotValidError

from app.config import DEMO_MODE
from app.database import get_session
from app.models import (
    User,
    SupplyLot,
    DemandOrder,
    VerificationCode,
    PasswordResetToken,
    Match,
    get_utc_now_naive,
)
from app.services.auth import (
    create_access_token,
    decode_access_token,
    generate_otp,
    hash_password,
    verify_password,
)
from app.services.sms import send_sms
from app.services.notification_service import (
    get_unread_count,
    get_latest_notifications,
)
from app.services.geocode import reverse_geocode
from app.services.validation import validate_ghana_phone

templates = Jinja2Templates(directory="app/templates")
router = APIRouter()


def get_current_user_from_cookie(request: Request, db: Session = Depends(get_session)) -> Optional[User]:
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        payload = decode_access_token(token)
        user_id = int(payload["sub"])
        user = db.get(User, user_id)
        return user if user and user.is_active else None
    except Exception:
        return None


def flash(request: Request, message: str, category: str = "info") -> None:
    if not hasattr(request.state, "flash_messages"):
        request.state.flash_messages = []
    request.state.flash_messages.append((category, message))


def get_flash(request: Request):
    return getattr(request.state, "flash_messages", [])


def template_context(request: Request, **kwargs) -> dict:
    """Return the context dict for public pages (no notification data)."""
    return {
        "request": request,
        "flash_messages": get_flash(request),
        "config": {"DEMO_MODE": DEMO_MODE},
        **kwargs,
    }


def get_base_context(request: Request, db: Session, user: Optional[User] = None) -> dict:
    """Return a context dict that includes notification data for authenticated pages."""
    context = {
        "request": request,
        "current_user": user,
        "flash_messages": get_flash(request),
        "config": {"DEMO_MODE": DEMO_MODE},
    }
    if user:
        context["unread_count"] = get_unread_count(db, user.id)
        context["latest_notifications"] = get_latest_notifications(db, user.id, limit=10)
    return context


# ---------------------------------------------------------------------------
# Password policy validation helper (UI form)
# ---------------------------------------------------------------------------
def validate_password_policy(password: str) -> str:
    """Return an error message if password doesn't meet policy, else empty string."""
    if len(password) < 8:
        return "Password must be at least 8 characters long."
    if len(password) > 72:
        return "Password must be at most 72 characters long."
    if not re.search(r'[a-z]', password):
        return "Password must include at least one lowercase letter."
    if not re.search(r'[A-Z]', password):
        return "Password must include at least one uppercase letter."
    if not re.search(r'\d', password):
        return "Password must include at least one digit."
    if not re.search(r'[^a-zA-Z\d]', password):
        return "Password must include at least one special character (e.g., !@#$%)."
    return ""


def _load_places():
    places_path = Path(__file__).resolve().parent.parent.parent / "data" / "ghana_places.json"
    places = {}
    try:
        with open(places_path, "r", encoding="utf-8") as f:
            places = json.load(f)
    except Exception:
        pass
    return places


# ---------------------------------------------------------------------------
# Public Pages (no auth required)
# ---------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
async def landing_page(request: Request):
    return templates.TemplateResponse("landing.html",
        template_context(request, current_user=None))


@router.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    return templates.TemplateResponse("about.html",
        template_context(request, current_user=None))


@router.get("/support", response_class=HTMLResponse)
async def support_page(request: Request):
    return templates.TemplateResponse("support.html",
        template_context(request, current_user=None))


@router.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request):
    """Terms of Service page."""
    return templates.TemplateResponse("legal/terms.html",
        template_context(request, current_user=None))


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    """Privacy Policy page."""
    return templates.TemplateResponse("legal/privacy.html",
        template_context(request, current_user=None))


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("auth/login.html",
        template_context(request, current_user=None, form_data={}))


@router.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    phone: str = Form(...),
    password: str = Form(...),
    remember: bool = Form(False),
    db: Session = Depends(get_session),
):
    form_data = {
        "phone": phone.strip(),
        "remember": remember,
    }

    # Validate phone format
    try:
        phone = validate_ghana_phone(phone)
    except ValueError as e:
        flash(request, str(e), "danger")
        return templates.TemplateResponse("auth/login.html",
            template_context(request, current_user=None, form_data=form_data))

    user = db.exec(select(User).where(User.phone == phone)).first()
    if not user:
        # Generic error to prevent user enumeration
        flash(request, "Invalid phone number or password.", "danger")
        return templates.TemplateResponse("auth/login.html",
            template_context(request, current_user=None, form_data=form_data))

    # Check if account is temporarily locked
    now = get_utc_now_naive()
    if user.locked_until and now < user.locked_until:
        flash(request, "Account temporarily locked due to too many failed attempts. Please try again later.", "danger")
        return templates.TemplateResponse("auth/login.html",
            template_context(request, current_user=None, form_data=form_data))

    # Verify password
    if not verify_password(password, user.password_hash):
        user.failed_login_attempts += 1
        user.last_failed_login_at = now

        if user.failed_login_attempts >= 5:
            user.locked_until = now + timedelta(minutes=15)
            user.failed_login_attempts = 0  # reset after locking
            flash(request, "Too many failed attempts. Account locked for 15 minutes.", "danger")
        else:
            remaining = 5 - user.failed_login_attempts
            flash(request, f"Invalid phone number or password. {remaining} attempt(s) remaining.", "danger")

        db.add(user)
        db.commit()
        return templates.TemplateResponse("auth/login.html",
            template_context(request, current_user=None, form_data=form_data))

    # Account status checks after password correct
    if not user.is_active:
        # Distinguish unverified vs deactivated
        pending_vc = db.exec(
            select(VerificationCode).where(
                VerificationCode.user_id == user.id,
                VerificationCode.purpose == "signup",
                VerificationCode.used == False,
            )
        ).first()
        if pending_vc:
            flash(request, "Phone not verified. Please verify your phone.", "warning")
            return RedirectResponse(url=f"/verify-phone?phone={phone}", status_code=status.HTTP_303_SEE_OTHER)
        else:
            flash(request, "This account has been suspended. Please contact support.", "danger")
            return templates.TemplateResponse("auth/login.html",
                template_context(request, current_user=None, form_data=form_data))

    # Success: reset failed attempts
    user.failed_login_attempts = 0
    user.last_failed_login_at = None
    user.locked_until = None
    db.add(user)
    db.commit()

    token = create_access_token(user.id, user.role)
    redirect_url = f"/{user.role}/dashboard" if user.role != "admin" else "/admin/dashboard"
    resp = RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)

    if remember:
        resp.set_cookie("access_token", token, httponly=True, max_age=2592000, samesite='lax')
    else:
        resp.set_cookie("access_token", token, httponly=True, samesite='lax')

    return resp


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    places = _load_places()
    return templates.TemplateResponse("auth/register.html",
        template_context(request, current_user=None, places=places, form_data={}))


@router.post("/register", response_class=HTMLResponse)
async def register_post(
    request: Request,
    name: str = Form(...),
    phone: str = Form(...),
    role: str = Form(...),
    region: str = Form(...),
    district: str = Form(""),
    town: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    email: Optional[str] = Form(None),
    accepted_terms: bool = Form(False),
    db: Session = Depends(get_session),
):
    places = _load_places()
    form_data = {
        "name": name.strip(),
        "phone": phone.strip(),
        "role": role,
        "region": region,
        "district": district,
        "town": town,
        "email": email.strip() if email else "",
        "accepted_terms": accepted_terms,
    }

    # --------------------------- Validate Name ---------------------------
    name = name.strip()
    if not name or len(name) < 2:
        flash(request, "Full name must be at least 2 characters long.", "danger")
        return templates.TemplateResponse("auth/register.html",
            template_context(request, current_user=None, places=places, form_data=form_data))

    # --------------------------- Validate Phone ---------------------------
    try:
        phone = validate_ghana_phone(phone)
    except ValueError as e:
        flash(request, str(e), "danger")
        return templates.TemplateResponse("auth/register.html",
            template_context(request, current_user=None, places=places, form_data=form_data))

    # --------------------------- Validate Email ---------------------------
    email = email.strip() if email else None
    if email:
        try:
            # check_deliverability=False to avoid DNS lookups during tests/offline
            validate_email(email, check_deliverability=False)
        except EmailNotValidError:
            flash(request, "Please enter a valid email address.", "danger")
            return templates.TemplateResponse("auth/register.html",
                template_context(request, current_user=None, places=places, form_data=form_data))

    # --------------------------- Validate Role ---------------------------
    if role not in ("farmer", "vendor"):
        flash(request, "Please select a valid account role.", "danger")
        return templates.TemplateResponse("auth/register.html",
            template_context(request, current_user=None, places=places, form_data=form_data))

    # --------------------------- Validate Location ---------------------------
    if not region or region == "Select Region":
        flash(request, "Please select a valid region.", "danger")
        return templates.TemplateResponse("auth/register.html",
            template_context(request, current_user=None, places=places, form_data=form_data))
    if not district or district == "Select District":
        flash(request, "Please select a valid district.", "danger")
        return templates.TemplateResponse("auth/register.html",
            template_context(request, current_user=None, places=places, form_data=form_data))
    if not town or town == "Select Town":
        flash(request, "Please select a valid town or city.", "danger")
        return templates.TemplateResponse("auth/register.html",
            template_context(request, current_user=None, places=places, form_data=form_data))

    # --------------------------- Validate Password ---------------------------
    error = validate_password_policy(password)
    if error:
        flash(request, error, "danger")
        return templates.TemplateResponse("auth/register.html",
            template_context(request, current_user=None, places=places, form_data=form_data))

    if password != confirm_password:
        flash(request, "Passwords do not match.", "danger")
        return templates.TemplateResponse("auth/register.html",
            template_context(request, current_user=None, places=places, form_data=form_data))

    # --------------------------- Validate Consent ---------------------------
    if not accepted_terms:
        flash(request, "You must accept the Terms of Service and Privacy Policy to register.", "danger")
        return templates.TemplateResponse("auth/register.html",
            template_context(request, current_user=None, places=places, form_data=form_data))

    # --------------------------- Check Duplicate Phone ---------------------------
    existing = db.exec(select(User).where(User.phone == phone)).first()
    if existing:
        flash(request, "Phone already registered. Please login.", "danger")
        return templates.TemplateResponse("auth/register.html",
            template_context(request, current_user=None, places=places, form_data=form_data))

    # --------------------------- Check Duplicate Email ---------------------------
    if email:
        email_existing = db.exec(select(User).where(User.email == email)).first()
        if email_existing:
            flash(request, "Email already registered. Please use a different email.", "danger")
            return templates.TemplateResponse("auth/register.html",
                template_context(request, current_user=None, places=places, form_data=form_data))

    # --------------------------- Create User ---------------------------
    user = User(
        name=name,
        phone=phone,
        password_hash=hash_password(password),
        role=role,
        town=town,
        region=region,
        district=district,
        email=email,  # already None or valid
        is_active=False,
        accepted_terms_at=get_utc_now_naive(),
    )
    db.add(user)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, "An account with this phone or email already exists.", "danger")
        return templates.TemplateResponse("auth/register.html",
            template_context(request, current_user=None, places=places, form_data=form_data))

    db.refresh(user)

    code = generate_otp()
    expires = get_utc_now_naive() + timedelta(minutes=10)
    vc = VerificationCode(user_id=user.id, code=code, purpose="signup", expires_at=expires)
    db.add(vc)
    db.commit()

    send_sms(phone, f"Your ASPEN verification code is: {code}", user_id=user.id, db_session=db)
    return RedirectResponse(url=f"/verify-phone?phone={phone}", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Phone Verification
# ---------------------------------------------------------------------------
@router.get("/verify-phone", response_class=HTMLResponse)
async def verify_phone_page(request: Request, phone: str = "", db: Session = Depends(get_session)):
    try:
        phone = validate_ghana_phone(phone)
    except ValueError:
        phone = phone.replace(' ', '')

    demo_code = ""
    if DEMO_MODE and phone:
        user = db.exec(select(User).where(User.phone == phone)).first()
        if user:
            vc = db.exec(
                select(VerificationCode)
                .where(
                    VerificationCode.user_id == user.id,
                    VerificationCode.purpose == "signup",
                    VerificationCode.used == False,
                )
                .order_by(VerificationCode.created_at.desc())
            ).first()
            if vc:
                demo_code = vc.code

    return templates.TemplateResponse(
        "auth/verify_phone.html",
        template_context(request, current_user=None, phone=phone, demo_code=demo_code),
    )


@router.post("/verify-phone", response_class=HTMLResponse)
async def verify_phone_post(
    request: Request,
    code: str = Form(...),
    phone: str = Form(""),
    db: Session = Depends(get_session),
):
    # Validate phone format
    try:
        phone = validate_ghana_phone(phone)
    except ValueError as e:
        flash(request, str(e), "danger")
        return templates.TemplateResponse("auth/verify_phone.html",
            template_context(request, current_user=None, phone=phone, demo_code=""))

    # Validate code format (6 digits)
    if not re.fullmatch(r'\d{6}', code):
        flash(request, "Please enter a valid 6-digit verification code.", "danger")
        return templates.TemplateResponse("auth/verify_phone.html",
            template_context(request, current_user=None, phone=phone, demo_code=""))

    user = db.exec(select(User).where(User.phone == phone)).first()
    if not user:
        flash(request, "User not found. Please register again.", "danger")
        return templates.TemplateResponse("auth/verify_phone.html",
            template_context(request, current_user=None, phone=phone, demo_code=""))

    # If user already active, redirect to login
    if user.is_active:
        flash(request, "Phone already verified. Please login.", "success")
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    # Fetch latest unused signup code
    vc = db.exec(
        select(VerificationCode)
        .where(
            VerificationCode.user_id == user.id,
            VerificationCode.purpose == "signup",
            VerificationCode.used == False,
        )
        .order_by(VerificationCode.created_at.desc())
    ).first()

    if not vc:
        flash(request, "No verification code found. Please request a new one.", "danger")
        return templates.TemplateResponse("auth/verify_phone.html",
            template_context(request, current_user=None, phone=phone, demo_code=""))

    # Check expiration
    if vc.expires_at < get_utc_now_naive():
        flash(request, "Code expired. Please request a new one.", "danger")
        return templates.TemplateResponse("auth/verify_phone.html",
            template_context(request, current_user=None, phone=phone, demo_code=""))

    # Check brute-force attempts
    if vc.attempts >= 5:
        flash(request, "Too many failed attempts. Please request a new code.", "danger")
        return templates.TemplateResponse("auth/verify_phone.html",
            template_context(request, current_user=None, phone=phone, demo_code=""))

    # Check code match
    if vc.code == code:
        vc.used = True
        vc.attempts = 0  # reset attempts on success
        vc.last_attempt_at = get_utc_now_naive()
        user.is_active = True
        db.add(vc)
        db.add(user)
        db.commit()
        flash(request, "Phone verified. Please login.", "success")
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    # Code does not match: record failed attempt
    vc.attempts += 1
    vc.last_attempt_at = get_utc_now_naive()
    db.add(vc)
    db.commit()

    remaining = max(0, 5 - vc.attempts)
    flash(request, f"Invalid code. {remaining} attempt(s) remaining.", "danger")
    return templates.TemplateResponse("auth/verify_phone.html",
        template_context(request, current_user=None, phone=phone, demo_code=""))


@router.post("/verify-phone/resend")
async def verify_phone_resend_post(
    phone: str = Form(...),
    db: Session = Depends(get_session),
):
    # Validate phone format
    try:
        phone = validate_ghana_phone(phone)
    except ValueError as e:
        return {"message": str(e)}

    user = db.exec(select(User).where(User.phone == phone)).first()
    if not user:
        return {"message": "If that phone is registered, a new code has been sent."}

    if user.is_active:
        return {"message": "Account is already verified. Please login."}

    now = get_utc_now_naive()

    # 1. Rate limiting: check most recent unused signup code
    recent_code = db.exec(
        select(VerificationCode)
        .where(
            VerificationCode.user_id == user.id,
            VerificationCode.purpose == "signup",
            VerificationCode.used == False,
        )
        .order_by(VerificationCode.created_at.desc())
    ).first()

    if recent_code:
        # Cooldown based on last_resend_at or created_at
        last_time = recent_code.last_resend_at or recent_code.created_at
        age_seconds = (now - last_time).total_seconds()
        if age_seconds < 60:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Please wait before requesting another code.",
            )

        # Max resends per hour: count codes created in last hour
        one_hour_ago = now - timedelta(hours=1)
        recent_codes_count = db.exec(
            select(func.count(VerificationCode.id))
            .where(
                VerificationCode.user_id == user.id,
                VerificationCode.purpose == "signup",
                VerificationCode.used == False,
                VerificationCode.created_at >= one_hour_ago,
            )
        ).one()
        if recent_codes_count >= 3:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many resend attempts. Please try again later.",
            )

    # 2. Invalidate all previous unused signup codes for this user
    old_codes = db.exec(
        select(VerificationCode)
        .where(
            VerificationCode.user_id == user.id,
            VerificationCode.purpose == "signup",
            VerificationCode.used == False,
        )
    ).all()

    for old_code in old_codes:
        old_code.used = True
        db.add(old_code)

    # 3. Create and send new code
    code = generate_otp()
    expires = now + timedelta(minutes=10)
    new_vc = VerificationCode(
        user_id=user.id,
        code=code,
        purpose="signup",
        expires_at=expires,
        resend_count=0,
        last_resend_at=now,
    )
    db.add(new_vc)
    db.commit()

    send_sms(
        phone=phone,
        message=f"Your ASPEN verification code is: {code}",
        user_id=user.id,
        db_session=db,
    )

    return {"message": "A new verification code has been sent."}


# ---------------------------------------------------------------------------
# Forgot Password
# ---------------------------------------------------------------------------
@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse("auth/forgot_password.html",
        template_context(request, current_user=None, form_data={}))


@router.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password_post(
    request: Request,
    phone: str = Form(...),
    db: Session = Depends(get_session),
):
    form_data = {"phone": phone.strip()}

    # Validate phone format
    try:
        phone = validate_ghana_phone(phone)
    except ValueError as e:
        flash(request, str(e), "danger")
        return templates.TemplateResponse("auth/forgot_password.html",
            template_context(request, current_user=None, form_data=form_data))

    user = db.exec(select(User).where(User.phone == phone)).first()
    now = get_utc_now_naive()

    if user:
        # Check if account is restricted/suspended
        if not user.is_active:
            flash(request, "This account is currently restricted. Please contact support.", "danger")
            return templates.TemplateResponse("auth/forgot_password.html",
                template_context(request, current_user=None, form_data=form_data))

        # Rate limiting: check recent unused reset token for cooldown
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
                flash(request, "Please wait before requesting another reset code.", "danger")
                return templates.TemplateResponse(
                    "auth/forgot_password.html",
                    template_context(request, current_user=None, form_data=form_data),
                )

        # Max resends per hour: count unused tokens created in last hour
        one_hour_ago = now - timedelta(hours=1)
        recent_tokens_count = db.exec(
            select(func.count(PasswordResetToken.id))
            .where(
                PasswordResetToken.user_id == user.id,
                PasswordResetToken.used == False,
                PasswordResetToken.created_at >= one_hour_ago,
            )
        ).one()
        if recent_tokens_count >= 3:
            flash(request, "Too many reset codes requested. Please try again later.", "danger")
            return templates.TemplateResponse("auth/forgot_password.html",
                template_context(request, current_user=None, form_data=form_data))

        # Invalidate all previous unused reset tokens for this user
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

        # Create new reset code
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

    # Always return ambiguous success
    flash(request, "If that phone is registered, a reset code has been sent.", "success")
    return RedirectResponse(url=f"/reset-password?phone={phone}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/forgot-password/resend")
async def forgot_password_resend_post(
    phone: str = Form(...),
    db: Session = Depends(get_session),
):
    # Validate phone format
    try:
        phone = validate_ghana_phone(phone)
    except ValueError as e:
        return {"message": str(e)}

    user = db.exec(select(User).where(User.phone == phone)).first()
    if not user:
        return {"message": "If that phone is registered, a reset code has been sent."}

    if not user.is_active:
        return {"message": "This account is currently restricted. Please contact support."}

    now = get_utc_now_naive()

    # Rate limiting: check recent unused reset token
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

    # Max resends per hour
    one_hour_ago = now - timedelta(hours=1)
    recent_tokens_count = db.exec(
        select(func.count(PasswordResetToken.id))
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used == False,
            PasswordResetToken.created_at >= one_hour_ago,
        )
    ).one()
    if recent_tokens_count >= 3:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many reset codes requested. Please try again later.",
        )

    # Invalidate old unused tokens
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

    # Create new token
    code = generate_otp()
    expires = now + timedelta(minutes=10)
    new_token = PasswordResetToken(
        user_id=user.id,
        token=code,
        expires_at=expires,
    )
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
# Reset Password
# ---------------------------------------------------------------------------
@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request, phone: str = "", db: Session = Depends(get_session)):
    try:
        phone = validate_ghana_phone(phone)
    except ValueError:
        phone = phone.replace(' ', '')

    demo_code = ""
    if DEMO_MODE and phone:
        user = db.exec(select(User).where(User.phone == phone)).first()
        if user:
            token = db.exec(
                select(PasswordResetToken)
                .where(
                    PasswordResetToken.user_id == user.id,
                    PasswordResetToken.used == False,
                )
                .order_by(PasswordResetToken.created_at.desc())
            ).first()
            if token:
                demo_code = token.token

    return templates.TemplateResponse("auth/reset_password.html",
        template_context(request, current_user=None, phone=phone, demo_code=demo_code))


@router.post("/reset-password", response_class=HTMLResponse)
async def reset_password_post(
    request: Request,
    code: str = Form(...),
    phone: str = Form(...),
    new_password: str = Form(...),
    confirm_new_password: str = Form(...),
    db: Session = Depends(get_session),
):
    # Validate phone format
    try:
        phone = validate_ghana_phone(phone)
    except ValueError as e:
        flash(request, str(e), "danger")
        return RedirectResponse(url="/forgot-password", status_code=status.HTTP_303_SEE_OTHER)

    # Validate code format (6 digits)
    if not re.fullmatch(r'\d{6}', code):
        flash(request, "Please enter a valid 6-digit reset code.", "danger")
        return templates.TemplateResponse("auth/reset_password.html",
            template_context(request, current_user=None, phone=phone, demo_code=""))

    # Validate new password policy
    error = validate_password_policy(new_password)
    if error:
        flash(request, error, "danger")
        return templates.TemplateResponse("auth/reset_password.html",
            template_context(request, current_user=None, phone=phone, demo_code=""))

    # Check passwords match
    if new_password != confirm_new_password:
        flash(request, "Passwords do not match.", "danger")
        return templates.TemplateResponse("auth/reset_password.html",
            template_context(request, current_user=None, phone=phone, demo_code=""))

    user = db.exec(select(User).where(User.phone == phone)).first()
    if not user:
        flash(request, "Invalid request", "danger")
        return RedirectResponse(url="/forgot-password", status_code=status.HTTP_303_SEE_OTHER)

    # Optional: prevent same password as old
    if verify_password(new_password, user.password_hash):
        flash(request, "Your new password cannot be the same as your old password.", "danger")
        return templates.TemplateResponse("auth/reset_password.html",
            template_context(request, current_user=None, phone=phone, demo_code=""))

    # Fetch latest unused reset token (regardless of code)
    reset_token = db.exec(
        select(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used == False,
        )
        .order_by(PasswordResetToken.created_at.desc())
    ).first()

    # Generic error for no token or expired
    if not reset_token or reset_token.expires_at < get_utc_now_naive():
        flash(request, "Invalid or expired reset code.", "danger")
        return templates.TemplateResponse("auth/reset_password.html",
            template_context(request, current_user=None, phone=phone, demo_code=""))

    # Check brute-force attempts BEFORE comparing code
    if reset_token.attempts >= 3:
        # Too many failed attempts: invalidate token and force restart
        reset_token.used = True
        db.add(reset_token)
        db.commit()
        flash(request, "Too many failed attempts. Please request a new reset code.", "danger")
        return RedirectResponse(url="/forgot-password", status_code=status.HTTP_303_SEE_OTHER)

    # Compare code
    if reset_token.token != code:
        # Increment failed attempts
        reset_token.attempts += 1
        reset_token.last_attempt_at = get_utc_now_naive()
        db.add(reset_token)
        db.commit()
        flash(request, "Invalid or expired reset code.", "danger")
        return templates.TemplateResponse("auth/reset_password.html",
            template_context(request, current_user=None, phone=phone, demo_code=""))

    # Code correct: reset password and invalidate token
    user.password_hash = hash_password(new_password)
    reset_token.used = True
    reset_token.attempts = 0
    reset_token.last_attempt_at = get_utc_now_naive()
    db.add(user)
    db.add(reset_token)
    db.commit()

    flash(request, "Password reset. Please login.", "success")
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------
@router.get("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie("access_token")
    return resp


# ---------------------------------------------------------------------------
# Profile (protected, passes access_token & places data + notification data)
# ---------------------------------------------------------------------------
@router.get("/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    current_user: User = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_session),
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    places = _load_places()
    access_token = request.cookies.get("access_token", "")
    base = get_base_context(request, db, current_user)
    extra = {"access_token": access_token, "places": places}
    return templates.TemplateResponse("account/profile.html", {**base, **extra})


# ---------------------------------------------------------------------------
# Farmer Dashboard (protected, passes access_token + computed counts)
# ---------------------------------------------------------------------------
@router.get("/farmer/dashboard", response_class=HTMLResponse)
async def farmer_dashboard(
    request: Request,
    current_user: User = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_session),
):
    if not current_user or current_user.role != "farmer":
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    lots = db.exec(
        select(SupplyLot)
        .options(selectinload(SupplyLot.matches))
        .where(SupplyLot.farmer_id == current_user.id)
    ).all()

    access_token = request.cookies.get("access_token", "")

    active_lots = sum(1 for l in lots if l.status == "Open")
    matched_lots = sum(1 for l in lots if l.status in ("Matched", "Confirmed", "In Transit"))
    now = get_utc_now_naive()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    closed_today_kg = db.exec(
        select(func.sum(Match.quantity_kg))
        .join(SupplyLot, Match.supply_lot_id == SupplyLot.id)
        .where(
            SupplyLot.farmer_id == current_user.id,
            Match.status == "Closed",
            Match.closed_at >= today_start
        )
    ).one_or_none() or 0.0
    closed_today_str = f"{closed_today_kg} kg"

    base = get_base_context(request, db, current_user)
    extra = {
        "lots": lots,
        "now": now,
        "access_token": access_token,
        "active_lots": active_lots,
        "matched_lots": matched_lots,
        "closed_today": closed_today_str,
    }
    return templates.TemplateResponse("farmer/dashboard.html", {**base, **extra})


# ---------------------------------------------------------------------------
# Farmer Add Lot (protected, passes access_token, places data & crop data)
# ---------------------------------------------------------------------------
@router.get("/farmer/add", response_class=HTMLResponse)
async def farmer_add_lot(
    request: Request,
    current_user: User = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_session),
):
    if not current_user or current_user.role != "farmer":
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    places = _load_places()
    crops_path = Path(__file__).resolve().parent.parent.parent / "data" / "crops.json"
    crop_data = {}
    try:
        with open(crops_path, "r", encoding="utf-8") as f:
            crop_data = json.load(f)
    except Exception:
        pass

    access_token = request.cookies.get("access_token", "")
    base = get_base_context(request, db, current_user)
    extra = {"access_token": access_token, "places": places, "crop_data": crop_data}
    return templates.TemplateResponse("farmer/add_lot.html", {**base, **extra})


# ---------------------------------------------------------------------------
# Vendor Dashboard (protected, passes access_token + computed counts)
# ---------------------------------------------------------------------------
@router.get("/vendor/dashboard", response_class=HTMLResponse)
async def vendor_dashboard(
    request: Request,
    current_user: User = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_session),
):
    if not current_user or current_user.role != "vendor":
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    orders = db.exec(select(DemandOrder).where(DemandOrder.vendor_id == current_user.id)).all()
    access_token = request.cookies.get("access_token", "")

    now = get_utc_now_naive()
    orders_with_shelf_life = []

    for order in orders:
        order_info = {
            "id": order.id,
            "crop_type": order.crop_type,
            "quantity_kg": order.quantity_kg,
            "min_shelf_life_h": order.min_shelf_life_h,
            "lat": order.lat,
            "lon": order.lon,
            "location_label": order.location_label,
            "notes": order.notes,
            "status": order.status,
            "created_at": order.created_at,
            "remaining_hours": None,
            "match_id": None,
            "vendor_accepted": False,
            "accept_deadline": None,
            "dispatch_deadline": None,
            "dispute_resolution": None,
        }

        active_match = db.exec(
            select(Match)
            .where(
                Match.demand_order_id == order.id,
                Match.status.in_(["Matched", "Confirmed", "In Transit", "Disputed"])
            )
            .order_by(Match.created_at.desc())
        ).first()

        if active_match:
            order_info["match_id"] = active_match.id
            order_info["vendor_accepted"] = active_match.vendor_accepted
            order_info["accept_deadline"] = active_match.accept_deadline
            order_info["dispatch_deadline"] = active_match.dispatch_deadline

            lot = db.get(SupplyLot, active_match.supply_lot_id)
            if lot and lot.spoilage_time:
                delta_h = (lot.spoilage_time - now).total_seconds() / 3600.0
                order_info["remaining_hours"] = round(max(0.0, delta_h), 1)

        resolved_match = db.exec(
            select(Match)
            .where(
                Match.demand_order_id == order.id,
                Match.dispute_resolution.isnot(None)
            )
            .order_by(Match.created_at.desc())
        ).first()
        if resolved_match:
            order_info["dispute_resolution"] = resolved_match.dispute_resolution

        orders_with_shelf_life.append(order_info)

    active_orders = sum(1 for o in orders if o.status == "Open")
    matched_orders = sum(1 for o in orders if o.status in ("Matched", "Confirmed", "In Transit"))
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    received_today_kg = db.exec(
        select(func.sum(Match.quantity_kg))
        .join(DemandOrder, Match.demand_order_id == DemandOrder.id)
        .where(
            DemandOrder.vendor_id == current_user.id,
            Match.status == "Closed",
            Match.closed_at >= today_start
        )
    ).one_or_none() or 0.0
    received_today_str = f"{received_today_kg} kg"

    base = get_base_context(request, db, current_user)
    extra = {
        "orders": orders_with_shelf_life,
        "access_token": access_token,
        "active_orders": active_orders,
        "matched_orders": matched_orders,
        "received_today": received_today_str,
    }
    return templates.TemplateResponse("vendor/dashboard.html", {**base, **extra})


# ---------------------------------------------------------------------------
# Vendor Add Order (protected, passes access_token, places data & crop data)
# ---------------------------------------------------------------------------
@router.get("/vendor/add", response_class=HTMLResponse)
async def vendor_add_order(
    request: Request,
    current_user: User = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_session),
):
    if not current_user or current_user.role != "vendor":
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    places = _load_places()
    crops_path = Path(__file__).resolve().parent.parent.parent / "data" / "crops.json"
    crop_data = {}
    try:
        with open(crops_path, "r", encoding="utf-8") as f:
            crop_data = json.load(f)
    except Exception:
        pass

    access_token = request.cookies.get("access_token", "")
    base = get_base_context(request, db, current_user)
    extra = {"access_token": access_token, "places": places, "crop_data": crop_data}
    return templates.TemplateResponse("vendor/add_order.html", {**base, **extra})


# ---------------------------------------------------------------------------
# Farmer History (protected)
# ---------------------------------------------------------------------------
@router.get("/farmer/history", response_class=HTMLResponse)
async def farmer_history(
    request: Request,
    current_user: User = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_session),
):
    if not current_user or current_user.role != "farmer":
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    records = db.exec(
        select(Match, SupplyLot, User)
        .join(SupplyLot, Match.supply_lot_id == SupplyLot.id)
        .join(DemandOrder, Match.demand_order_id == DemandOrder.id)
        .join(User, DemandOrder.vendor_id == User.id)
        .where(
            SupplyLot.farmer_id == current_user.id,
            or_(
                Match.status == "Closed",
                SupplyLot.status == "Spoiled"
            )
        )
        .order_by(SupplyLot.created_at.desc())
    ).all()

    history = []
    for m, lot, vendor in records:
        outcome = "Closed" if m.status == "Closed" else "Spoiled"
        close_type = None
        reason = None

        if outcome == "Closed":
            if m.dispute_resolution:
                close_type = "Admin Rejection"
                reason = m.dispute_resolution
            elif m.recorded_shelf_life_at_receipt_h is not None:
                close_type = "Confirmed Fresh"
                reason = None
            else:
                close_type = "Auto‑Closed"
                reason = None
        else:
            if m.dispute_resolution and m.dispute_resolution != "rejected":
                reason = m.dispute_resolution
            else:
                reason = "Spoiled automatically (time expired)"

        spoil_date = lot.spoiled_at or lot.spoilage_time

        history.append({
            "crop_type": lot.crop_type,
            "quantity_kg": m.quantity_kg,
            "outcome": outcome,
            "close_type": close_type,
            "date_str": (m.closed_at.strftime("%d %b %Y") if outcome == "Closed" and m.closed_at
                         else (spoil_date.strftime("%d %b %Y") if spoil_date else "N/A")),
            "date_iso": (m.closed_at.isoformat() if outcome == "Closed" and m.closed_at
                         else (spoil_date.isoformat() if spoil_date else "")),
            "vendor_name": vendor.name,
            "vendor_phone": vendor.phone,
            "distance_km": round(m.distance_km, 1) if m.distance_km is not None else None,
            "priority_score": m.priority_score,
            "spoilage_reason": reason,
        })

    unmatched_spoiled = db.exec(
        select(SupplyLot)
        .where(
            SupplyLot.farmer_id == current_user.id,
            SupplyLot.status == "Spoiled",
            ~SupplyLot.matches.any()
        )
        .order_by(SupplyLot.created_at.desc())
    ).all()

    for lot in unmatched_spoiled:
        spoil_date = lot.spoiled_at or lot.spoilage_time
        history.append({
            "crop_type": lot.crop_type,
            "quantity_kg": lot.quantity_kg,
            "outcome": "Spoiled",
            "close_type": None,
            "date_str": spoil_date.strftime("%d %b %Y") if spoil_date else "N/A",
            "date_iso": spoil_date.isoformat() if spoil_date else "",
            "vendor_name": None,
            "vendor_phone": None,
            "distance_km": None,
            "priority_score": None,
            "spoilage_reason": "Spoiled automatically (time expired) – no vendor match",
        })

    history.sort(key=lambda x: x["date_iso"] or "", reverse=True)

    access_token = request.cookies.get("access_token", "")
    base = get_base_context(request, db, current_user)
    extra = {"history": history, "access_token": access_token}
    return templates.TemplateResponse("farmer/history.html", {**base, **extra})


# ---------------------------------------------------------------------------
# Vendor History (protected)
# ---------------------------------------------------------------------------
@router.get("/vendor/history", response_class=HTMLResponse)
async def vendor_history(
    request: Request,
    current_user: User = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_session),
):
    if not current_user or current_user.role != "vendor":
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    closed_matches = db.exec(
        select(Match, DemandOrder, User)
        .join(DemandOrder, Match.demand_order_id == DemandOrder.id)
        .join(SupplyLot, Match.supply_lot_id == SupplyLot.id)
        .join(User, SupplyLot.farmer_id == User.id)
        .where(
            DemandOrder.vendor_id == current_user.id,
            Match.status == "Closed"
        )
        .order_by(Match.closed_at.desc())
    ).all()

    history = []
    for m, order, farmer in closed_matches:
        close_type = None
        reason = None

        if m.dispute_resolution:
            close_type = "Admin Rejection"
            reason = m.dispute_resolution
        elif m.recorded_shelf_life_at_receipt_h is not None:
            close_type = "Confirmed Fresh"
            reason = None
        else:
            close_type = "Auto‑Closed"
            reason = None

        history.append({
            "crop_type": order.crop_type,
            "quantity_kg": m.quantity_kg,
            "outcome": "Closed",
            "close_type": close_type,
            "date_str": m.closed_at.strftime("%d %b %Y") if m.closed_at else "N/A",
            "date_iso": m.closed_at.isoformat() if m.closed_at else "",
            "farmer_name": farmer.name,
            "farmer_phone": farmer.phone,
            "distance_km": round(m.distance_km, 1) if m.distance_km is not None else None,
            "priority_score": m.priority_score,
            "spoilage_reason": reason,
        })

    released_matches = db.exec(
        select(Match, DemandOrder, User)
        .join(DemandOrder, Match.demand_order_id == DemandOrder.id)
        .join(SupplyLot, Match.supply_lot_id == SupplyLot.id)
        .join(User, SupplyLot.farmer_id == User.id)
        .where(
            DemandOrder.vendor_id == current_user.id,
            Match.status == "Expired",
            SupplyLot.status == "Spoiled"
        )
        .order_by(Match.created_at.desc())
    ).all()

    for m, order, farmer in released_matches:
        reason = None
        if m.dispute_resolution and m.dispute_resolution != "rejected":
            reason = m.dispute_resolution
        else:
            reason = "Produce spoiled before delivery"

        spoil_date = db.get(SupplyLot, m.supply_lot_id)
        date_to_show = spoil_date.spoiled_at if spoil_date and spoil_date.spoiled_at else spoil_date.spoilage_time if spoil_date else None

        history.append({
            "crop_type": order.crop_type,
            "quantity_kg": m.quantity_kg,
            "outcome": "Released",
            "close_type": None,
            "date_str": date_to_show.strftime("%d %b %Y") if date_to_show else "N/A",
            "date_iso": date_to_show.isoformat() if date_to_show else "",
            "farmer_name": farmer.name,
            "farmer_phone": farmer.phone,
            "distance_km": round(m.distance_km, 1) if m.distance_km is not None else None,
            "priority_score": m.priority_score,
            "spoilage_reason": reason,
        })

    history.sort(key=lambda x: x["date_iso"] or "", reverse=True)

    access_token = request.cookies.get("access_token", "")
    base = get_base_context(request, db, current_user)
    extra = {"history": history, "access_token": access_token}
    return templates.TemplateResponse("vendor/history.html", {**base, **extra})


# ---------------------------------------------------------------------------
# API: Reverse Geocoding (used by frontend when GPS is captured)
# ---------------------------------------------------------------------------
@router.get("/api/geocode")
async def api_reverse_geocode(lat: float, lon: float):
    """
    Given GPS coordinates, attempt reverse geocoding using Nominatim.
    Returns the human-readable label if found, otherwise an empty string.
    """
    try:
        label = await reverse_geocode(lat, lon)
        return {"label": label or ""}
    except Exception:
        return {"label": ""}