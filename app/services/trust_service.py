"""
Trust Score Service
-------------------
Evaluates vendor spoilage‑claim behaviour and automatically flags
accounts whose claim rate exceeds the platform threshold.
"""

import logging
from typing import Optional

from sqlmodel import Session, select, func

from app.config import SPOILAGE_CLAIM_THRESHOLD_RATIO, SPOILAGE_CLAIM_MIN_ORDERS
from app.database import engine
from app.models import User, DemandOrder, Match, NotificationType
from app.services.sms import send_sms
from app.services.notification_service import create_notification

logger = logging.getLogger("aspen.trust_service")


def update_vendor_trust_scores(db: Optional[Session] = None) -> None:
    """
    Calculate every vendor's spoilage‑claim rate and flag those
    that exceed the threshold.

    The claim rate is defined as:
        (number of matches where the vendor reported spoilage) /
        (number of matches where the vendor was involved and the match reached a terminal state)

    Only vendors with at least SPOILAGE_CLAIM_MIN_ORDERS completed orders
    are evaluated, to avoid false positives for new accounts.
    """
    if db is None:
        with Session(engine) as session:
            _update_trust(session)
    else:
        _update_trust(db)


def _update_trust(db: Session) -> None:
    # Get all vendors (active or not, we may want to flag suspended ones too)
    vendors = db.exec(select(User).where(User.role == "vendor")).all()

    # Compute platform average claim rate (across all vendors with enough orders)
    total_claims_all = 0
    total_orders_all = 0

    vendor_stats = []  # to avoid repeated queries

    for vendor in vendors:
        # Count completed orders where this vendor was involved.
        # A match is terminal if it is Closed (delivered or rejected dispute)
        # or Expired (spoiled/lot expired and order released).
        completed_orders = db.exec(
            select(func.count(Match.id))
            .join(DemandOrder, Match.demand_order_id == DemandOrder.id)
            .where(
                DemandOrder.vendor_id == vendor.id,
                Match.status.in_(["Closed", "Expired"])  # actual terminal states
            )
        ).one() or 0

        # Count spoilage claims by this vendor.
        # A claim is counted when the match has a dispute_resolution that is
        # not null and not "rejected". This ensures only approved claims count.
        spoilage_claims = db.exec(
            select(func.count(Match.id))
            .join(DemandOrder, Match.demand_order_id == DemandOrder.id)
            .where(
                DemandOrder.vendor_id == vendor.id,
                Match.dispute_resolution.isnot(None),
                Match.dispute_resolution != "rejected"
            )
        ).one() or 0

        vendor_stats.append({
            "vendor": vendor,
            "completed": completed_orders,
            "claims": spoilage_claims,
        })

        if completed_orders >= SPOILAGE_CLAIM_MIN_ORDERS:
            total_orders_all += completed_orders
            total_claims_all += spoilage_claims

    # Platform average (if any qualifying vendors)
    platform_avg = (total_claims_all / total_orders_all) if total_orders_all > 0 else 0.0

    flagged_count = 0
    unflag_count = 0
    newly_flagged_vendors = []   # vendors flagged for the first time in this run

    for stat in vendor_stats:
        vendor = stat["vendor"]
        completed = stat["completed"]
        claims = stat["claims"]

        if completed < SPOILAGE_CLAIM_MIN_ORDERS:
            # Not enough history – do nothing (keep existing flag if any, but clear reason)
            if vendor.flagged:
                vendor.flagged = False
                vendor.flag_reason = None
                db.add(vendor)
                unflag_count += 1
            continue

        claim_rate = claims / completed if completed > 0 else 0.0

        if claim_rate > SPOILAGE_CLAIM_THRESHOLD_RATIO and claim_rate > platform_avg:
            # Flag the vendor
            if not vendor.flagged:
                vendor.flagged = True
                vendor.flag_reason = (
                    f"Spoilage claim rate ({claim_rate:.0%}) exceeds threshold "
                    f"({SPOILAGE_CLAIM_THRESHOLD_RATIO:.0%}) and platform average ({platform_avg:.0%})."
                )
                db.add(vendor)
                flagged_count += 1
                newly_flagged_vendors.append(vendor)
        else:
            # Unflag if currently flagged
            if vendor.flagged:
                vendor.flagged = False
                vendor.flag_reason = None
                db.add(vendor)
                unflag_count += 1

    # --- Create in-app notifications for newly flagged vendors BEFORE commit ---
    if newly_flagged_vendors:
        admins = db.exec(
            select(User).where(User.role == "admin", User.is_active == True)
        ).all()

        for admin in admins:
            for vendor in newly_flagged_vendors:
                reason = vendor.flag_reason or "suspicious spoilage claim pattern"
                create_notification(
                    db,
                    admin.id,
                    f"Vendor flagged: {vendor.name} ({vendor.phone}). Reason: {reason}",
                    NotificationType.WARNING,
                )

    # Commit flag changes and notifications atomically
    db.commit()
    logger.info(
        "Trust update: flagged %d vendors, unflagged %d. Platform avg claim rate: %.2f%%",
        flagged_count, unflag_count, platform_avg * 100
    )

    # Send admin SMS for any newly flagged vendors (best-effort, after commit)
    if newly_flagged_vendors:
        _send_vendor_flagged_sms(db, newly_flagged_vendors)


def _send_vendor_flagged_sms(db: Session, vendors: list) -> None:
    """
    Notify all active admins about newly flagged vendors.

    This is best-effort: SMS failures are logged but do not affect the
    trust evaluation or flagging transaction.
    """
    try:
        admins = db.exec(
            select(User).where(User.role == "admin", User.is_active == True)
        ).all()

        for admin in admins:
            if not admin.phone:
                continue

            for vendor in vendors:
                reason = vendor.flag_reason or "suspicious spoilage claim pattern"
                message = (
                    f"[ASPEN] Vendor flagged: {vendor.name} ({vendor.phone}). "
                    f"Reason: {reason}"
                )
                send_sms(
                    phone=admin.phone,
                    message=message,
                    user_id=admin.id,
                    db_session=db,
                )
    except Exception:
        logger.exception("Failed to send vendor flagged SMS notifications to admins")