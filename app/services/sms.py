"""
SMS sending service.
Supports:
  - Demo mode (logs to console & DB, no real SMS sent)
  - Africa's Talking real SMS provider

Provider‑specific details are set in app/config.py and loaded from environment variables.
"""

import logging
from typing import Optional

import httpx
from sqlmodel import Session, select

from app.config import (
    DEMO_MODE,
    SMS_PROVIDER,
    AFRICASTALKING_USERNAME,
    AFRICASTALKING_API_KEY,
    AFRICASTALKING_ENDPOINT,
    AFRICASTALKING_SENDER_ID,
)
from app.models import SmsLog, User

logger = logging.getLogger("aspen.sms")


def send_sms(phone: str, message: str, user_id: Optional[int] = None,
             db_session: Optional[Session] = None) -> str:
    """
    Attempt to send an SMS message.

    In DEMO_MODE, the message is only logged; no external API is called.

    When a real provider is configured (Africa's Talking), a POST request is sent
    to the configured endpoint.  If the request fails, a fallback log entry is
    created and the error is logged.

    Returns "sent", "simulated", or "failed".
    """

    # 1. Demo mode — just log it
    if DEMO_MODE:
        if db_session:
            _log_to_db(db_session, phone, message, user_id, "simulated")
        logger.info(f"[DEMO] SMS to {phone}: {message}")
        return "simulated"

    # 2. Real provider: Africa's Talking
    if SMS_PROVIDER == "africastalking":
        if not AFRICASTALKING_API_KEY:
            logger.warning("No Africa's Talking API key configured; falling back to demo mode")
            if db_session:
                _log_to_db(db_session, phone, message, user_id, "simulated")
            return "simulated"

        status = "failed"
        try:
            response = httpx.post(
                AFRICASTALKING_ENDPOINT,
                headers={
                    "apiKey": AFRICASTALKING_API_KEY,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "username": AFRICASTALKING_USERNAME,
                    "to": phone,
                    "message": message,
                    "from": AFRICASTALKING_SENDER_ID,
                },
                timeout=10.0,
            )
            if response.is_success:
                data = response.json()
                # Africa's Talking response structure:
                # {"SMSMessageData": {"Message": "...", "Recipients": [{"statusCode": 101, "status": "Success", ...}]}}
                recipients = data.get("SMSMessageData", {}).get("Recipients", [])
                if recipients:
                    recipient = recipients[0]
                    if recipient.get("status") == "Success":
                        status = "sent"
                        logger.info(f"SMS sent to {phone}")
                    else:
                        logger.error(
                            f"Africa's Talking SMS failed: {recipient.get('status')} "
                            f"(code {recipient.get('statusCode')})"
                        )
                else:
                    # No recipients in response; treat as sent if HTTP 200
                    status = "sent"
                    logger.info(f"SMS sent to {phone} (no recipient details)")
            else:
                logger.error(f"SMS API error: {response.status_code} {response.text}")
        except Exception:
            logger.exception("SMS sending failed for %s", phone)

    else:
        # Unknown provider
        logger.warning(f"Unknown SMS provider '{SMS_PROVIDER}'. Falling back to demo mode.")
        if db_session:
            _log_to_db(db_session, phone, message, user_id, "simulated")
        return "simulated"

    # 4. Always log to DB (if session provided)
    if db_session:
        _log_to_db(db_session, phone, message, user_id, status)

        # If the provider failed, notify admins (best-effort)
        if status == "failed":
            _notify_admins_of_provider_failure(db_session, phone)

    return status


def send_sms_background(phone: str, message: str,
                        user_id: Optional[int] = None) -> str:
    """
    Background-safe wrapper for send_sms.

    Opens its own database session so it can be safely called from
    FastAPI BackgroundTasks (or any context without an existing session).
    Ensures the SMS log is persisted even after the request response is sent.
    """
    from app.database import Session, engine

    with Session(engine) as db:
        return send_sms(
            phone=phone,
            message=message,
            user_id=user_id,
            db_session=db,
        )


def _log_to_db(session: Session, phone: str, message: str,
               user_id: Optional[int], status: str) -> None:
    """Create a persistent SmsLog entry."""
    log_entry = SmsLog(
        user_id=user_id,
        phone=phone,
        message=message,
        status=status,
    )
    session.add(log_entry)
    session.commit()


def _notify_admins_of_provider_failure(session: Session, original_phone: str) -> None:
    """
    Create admin SMS log entries alerting that the real SMS provider failed.

    This is best-effort: failures here are logged but never raised to the caller.
    """
    try:
        admins = session.exec(
            select(User).where(User.role == "admin", User.is_active == True)
        ).all()

        for admin in admins:
            if not admin.phone:
                continue

            admin_log = SmsLog(
                user_id=admin.id,
                phone=admin.phone,
                message=(
                    f"[ASPEN] SMS provider failure while sending to {original_phone}. "
                    f"Please check SMS gateway."
                ),
                status="failed",
            )
            session.add(admin_log)

        session.commit()
    except Exception:
        logger.exception("Failed to notify admins about SMS provider failure")