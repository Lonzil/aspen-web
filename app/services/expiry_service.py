"""
Auto‑Expiry Service
-------------------
Periodically checks active matches and open lots, and expires them
according to configurable rules.

Includes:
  - Stale match expiry (time‑based + spoilage‑based)
  - Acceptance deadline expiry (Phase 1 – two‑party acceptance timer)
  - Dispatch deadline expiry (Phase 2 – farmer dispatch timer)
  - Delivery auto‑close guard (Phase 3 – vendor inactivity timer)
  - Ultimate zero‑shelf‑life rule (Phase 6 – spoilage of pending lots)
  - Open lot spoilage expiry (now uses SPOILED status)
"""

import logging
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app.config import (
    STALE_MATCH_TIMEOUT_MINUTES,
    DISPATCH_BUFFER_HOURS,
    AVG_SPEED_KMPH,
)
from app.database import engine
from app.engine.feasibility import remaining_shelf_life_h, is_feasible
from app.engine.geo import travel_time_h
from app.models import (
    Match,
    SupplyLot,
    DemandOrder,
    LotStatus,
    OrderStatus,
    User,
    NotificationType,
    get_utc_now_naive,
)
from app.services.sms import send_sms
from app.services.notification_service import create_notification

logger = logging.getLogger("aspen.expiry_service")


# ---------------------------------------------------------------------------
# Original stale‑match expiry (time + spoilage)
# ---------------------------------------------------------------------------
def expire_stale_matches(db: Optional[Session] = None) -> None:
    if db is None:
        with Session(engine) as session:
            _expire_stale_logic(session)
    else:
        _expire_stale_logic(db)


def _expire_stale_logic(db: Session) -> None:
    now = get_utc_now_naive()
    active_matches = db.exec(
        select(Match).where(Match.status == "Matched")
    ).all()

    match_expired = 0
    expired_match_ids = []

    for match in active_matches:
        lot = db.get(SupplyLot, match.supply_lot_id)
        order = db.get(DemandOrder, match.demand_order_id)
        if not lot or not order:
            continue

        stale = False
        if match.created_at:
            age_minutes = (now - match.created_at).total_seconds() / 60.0
            if age_minutes > STALE_MATCH_TIMEOUT_MINUTES:
                stale = True

        infeasible = False
        if lot.spoilage_time:
            remaining = remaining_shelf_life_h(
                lot.spoilage_time, now, DISPATCH_BUFFER_HOURS
            )
            travel = travel_time_h(match.distance_km, AVG_SPEED_KMPH)
            if not is_feasible(remaining, travel, order.min_shelf_life_h):
                infeasible = True

        if stale or infeasible:
            match.status = "Expired"
            match.farmer_accepted = False
            match.vendor_accepted = False
            lot.status = LotStatus.OPEN
            order.status = OrderStatus.OPEN
            db.add(match)
            db.add(lot)
            db.add(order)
            expired_match_ids.append(match.id)
            match_expired += 1

            # --- In-app notifications (before commit) ---
            farmer = db.get(User, lot.farmer_id)
            vendor = db.get(User, order.vendor_id)
            if farmer:
                create_notification(
                    db,
                    farmer.id,
                    f"Match expired: {match.quantity_kg:.0f}kg {lot.crop_type} with {vendor.name if vendor else 'Vendor'}. Your lot is back in the open pool.",
                    NotificationType.WARNING,
                )
            if vendor:
                create_notification(
                    db,
                    vendor.id,
                    f"Match expired: {match.quantity_kg:.0f}kg {lot.crop_type} from {farmer.name if farmer else 'Farmer'}. Your order is back in the open pool.",
                    NotificationType.WARNING,
                )

            logger.info(
                "Expired match %d (lot %d, order %d) – stale=%s infeasible=%s",
                match.id, lot.id, order.id, stale, infeasible
            )

    if match_expired:
        db.commit()
        logger.info("Expired %d stale/infeasible matches.", match_expired)

        for match_id in expired_match_ids:
            _send_expired_match_sms(db, match_id)


# ---------------------------------------------------------------------------
# Phase 1 – Acceptance deadline expiry
# ---------------------------------------------------------------------------
def expire_acceptance_deadlines(db: Optional[Session] = None) -> None:
    if db is None:
        with Session(engine) as session:
            _expire_acceptance_logic(session)
    else:
        _expire_acceptance_logic(db)


def _expire_acceptance_logic(db: Session) -> None:
    now = get_utc_now_naive()
    expired_matches = db.exec(
        select(Match).where(
            Match.status == "Matched",
            Match.accept_deadline.isnot(None),
            Match.accept_deadline < now,
        )
    ).all()

    count = 0
    expired_match_ids = []

    for match in expired_matches:
        lot = db.get(SupplyLot, match.supply_lot_id)
        order = db.get(DemandOrder, match.demand_order_id)
        if lot:
            lot.status = LotStatus.OPEN
            db.add(lot)
        if order:
            order.status = OrderStatus.OPEN
            db.add(order)
        match.status = "Expired"
        match.farmer_accepted = False
        match.vendor_accepted = False
        db.add(match)
        expired_match_ids.append(match.id)
        count += 1

        # --- In-app notifications (before commit) ---
        farmer = db.get(User, lot.farmer_id) if lot else None
        vendor = db.get(User, order.vendor_id) if order else None
        if farmer:
            create_notification(
                db,
                farmer.id,
                f"Match expired: acceptance deadline passed. Your lot is back in the open pool.",
                NotificationType.WARNING,
            )
        if vendor:
            create_notification(
                db,
                vendor.id,
                f"Match expired: acceptance deadline passed. Your order is back in the open pool.",
                NotificationType.WARNING,
            )

    if count:
        db.commit()
        logger.info("Expired %d matches due to acceptance deadline.", count)

        for match_id in expired_match_ids:
            _send_expired_match_sms(db, match_id)


# ---------------------------------------------------------------------------
# Phase 2 – Dispatch deadline expiry
# ---------------------------------------------------------------------------
def expire_dispatch_deadlines(db: Optional[Session] = None) -> None:
    if db is None:
        with Session(engine) as session:
            _expire_dispatch_logic(session)
    else:
        _expire_dispatch_logic(db)


def _expire_dispatch_logic(db: Session) -> None:
    now = get_utc_now_naive()
    expired_matches = db.exec(
        select(Match).where(
            Match.status == "Confirmed",
            Match.dispatch_deadline.isnot(None),
            Match.dispatch_deadline < now,
        )
    ).all()

    count = 0
    expired_match_ids = []

    for match in expired_matches:
        lot = db.get(SupplyLot, match.supply_lot_id)
        order = db.get(DemandOrder, match.demand_order_id)
        if lot:
            lot.status = LotStatus.OPEN
            db.add(lot)
        if order:
            order.status = OrderStatus.OPEN
            db.add(order)
        match.status = "Expired"
        match.farmer_accepted = False
        match.vendor_accepted = False
        db.add(match)
        expired_match_ids.append(match.id)
        count += 1

        # --- In-app notifications (before commit) ---
        farmer = db.get(User, lot.farmer_id) if lot else None
        vendor = db.get(User, order.vendor_id) if order else None
        if farmer:
            create_notification(
                db,
                farmer.id,
                f"Match expired: dispatch deadline passed. Your lot is back in the open pool.",
                NotificationType.WARNING,
            )
        if vendor:
            create_notification(
                db,
                vendor.id,
                f"Match expired: dispatch deadline passed. Your order is back in the open pool.",
                NotificationType.WARNING,
            )

    if count:
        db.commit()
        logger.info("Expired %d matches due to dispatch deadline.", count)

        for match_id in expired_match_ids:
            _send_expired_match_sms(db, match_id)


# ---------------------------------------------------------------------------
# Phase 3 – Delivery auto‑close guard
# ---------------------------------------------------------------------------
def expire_delivery_deadlines(db: Optional[Session] = None) -> None:
    if db is None:
        with Session(engine) as session:
            _expire_delivery_logic(session)
    else:
        _expire_delivery_logic(db)


def _expire_delivery_logic(db: Session) -> None:
    now = get_utc_now_naive()
    expired_matches = db.exec(
        select(Match).where(
            Match.status == "In Transit",
            Match.delivery_deadline.isnot(None),
            Match.delivery_deadline < now,
        )
    ).all()

    count = 0
    auto_closed_matches = []   # collect matches for SMS after commit

    for match in expired_matches:
        lot = db.get(SupplyLot, match.supply_lot_id)
        order = db.get(DemandOrder, match.demand_order_id)
        if lot:
            lot.status = LotStatus.CLOSED
            db.add(lot)
        if order:
            order.status = OrderStatus.CLOSED
            db.add(order)
        match.status = "Closed"
        match.closed_at = now
        db.add(match)
        auto_closed_matches.append(match)
        count += 1

        # --- In-app notifications (before commit) ---
        farmer = db.get(User, lot.farmer_id) if lot else None
        vendor = db.get(User, order.vendor_id) if order else None
        if farmer:
            create_notification(
                db,
                farmer.id,
                f"Delivery auto-closed: {match.quantity_kg:.0f}kg {lot.crop_type} to {vendor.name if vendor else 'Vendor'}. Delivery deadline passed.",
                NotificationType.INFO,
            )
        if vendor:
            create_notification(
                db,
                vendor.id,
                f"Delivery auto-closed: {match.quantity_kg:.0f}kg {lot.crop_type} from {farmer.name if farmer else 'Farmer'}. Delivery deadline passed.",
                NotificationType.INFO,
            )

    if count:
        db.commit()
        logger.info("Auto‑closed %d In‑Transit matches past delivery deadline.", count)

        for match in auto_closed_matches:
            _send_auto_close_sms(db, match)


def _send_auto_close_sms(db: Session, match: Match) -> None:
    """Send auto-close notifications to the farmer and vendor."""
    try:
        lot = db.get(SupplyLot, match.supply_lot_id)
        order = db.get(DemandOrder, match.demand_order_id)
        if not lot or not order:
            return

        farmer = db.get(User, lot.farmer_id)
        vendor = db.get(User, order.vendor_id)

        if farmer and farmer.phone:
            farmer_msg = (
                f"[ASPEN] Delivery auto-closed: {match.quantity_kg:.0f}kg {lot.crop_type} "
                f"to {vendor.name if vendor else 'Vendor'}. Delivery deadline passed."
            )
            send_sms(phone=farmer.phone, message=farmer_msg, user_id=farmer.id, db_session=db)

        if vendor and vendor.phone:
            vendor_msg = (
                f"[ASPEN] Delivery auto-closed: {match.quantity_kg:.0f}kg {lot.crop_type} "
                f"from {farmer.name if farmer else 'Farmer'}. Delivery deadline passed."
            )
            send_sms(phone=vendor.phone, message=vendor_msg, user_id=vendor.id, db_session=db)
    except Exception:
        logger.exception("Failed to send auto-close SMS for match %d", match.id)


def _send_expired_match_sms(db: Session, match_id: int) -> None:
    """Send expired-match notifications to the farmer and vendor."""
    try:
        match = db.get(Match, match_id)
        if not match:
            return

        lot = db.get(SupplyLot, match.supply_lot_id)
        order = db.get(DemandOrder, match.demand_order_id)
        if not lot or not order:
            return

        farmer = db.get(User, lot.farmer_id)
        vendor = db.get(User, order.vendor_id)

        if farmer and farmer.phone:
            farmer_msg = (
                f"[ASPEN] Match expired: {match.quantity_kg:.0f}kg {lot.crop_type} "
                f"with {vendor.name if vendor else 'Vendor'}. Your lot is back in the open pool."
            )
            send_sms(phone=farmer.phone, message=farmer_msg, user_id=farmer.id, db_session=db)

        if vendor and vendor.phone:
            vendor_msg = (
                f"[ASPEN] Match expired: {match.quantity_kg:.0f}kg {lot.crop_type} "
                f"from {farmer.name if farmer else 'Farmer'}. Your order is back in the open pool."
            )
            send_sms(phone=vendor.phone, message=vendor_msg, user_id=vendor.id, db_session=db)
    except Exception:
        logger.exception("Failed to send expired-match SMS for match %d", match_id)


# ---------------------------------------------------------------------------
# Phase 6 – Ultimate Zero‑Shelf‑Life Rule
# ---------------------------------------------------------------------------
def expire_spoiled_lots(db: Optional[Session] = None) -> None:
    if db is None:
        with Session(engine) as session:
            _expire_spoiled_logic(session)
    else:
        _expire_spoiled_logic(db)


def _expire_spoiled_logic(db: Session) -> None:
    now = get_utc_now_naive()
    # Fetch all lots that might be spoiled (pending states)
    pending_lots = db.exec(
        select(SupplyLot).where(
            SupplyLot.status.in_([
                LotStatus.OPEN,
                LotStatus.MATCHED,
                LotStatus.CONFIRMED,
            ])
        )
    ).all()

    spoiled_count = 0
    released_orders = 0
    spoiled_lot_ids = []

    for lot in pending_lots:
        if not lot.spoilage_time:
            continue
        if now >= lot.spoilage_time:            # naive vs naive comparison
            original_status = lot.status
            lot.status = LotStatus.SPOILED
            lot.spoiled_at = now                 # record the actual spoilage timestamp
            db.add(lot)
            spoiled_lot_ids.append(lot.id)
            spoiled_count += 1

            # --- In-app notification for farmer (before commit) ---
            farmer = db.get(User, lot.farmer_id)
            if farmer:
                create_notification(
                    db,
                    farmer.id,
                    f"Lot spoiled: {lot.quantity_kg:.0f}kg {lot.crop_type} has spoiled automatically and has been marked as Spoiled.",
                    NotificationType.DANGER,
                )

            logger.info("Ultimate rule: spoiled lot %d (crop %s)", lot.id, lot.crop_type)

            if original_status in (LotStatus.MATCHED, LotStatus.CONFIRMED):
                active_match = db.exec(
                    select(Match).where(
                        Match.supply_lot_id == lot.id,
                        Match.status.in_([LotStatus.MATCHED, LotStatus.CONFIRMED]),
                    )
                ).first()
                if active_match:
                    order = db.get(DemandOrder, active_match.demand_order_id)
                    if order:
                        order.status = OrderStatus.OPEN
                        db.add(order)
                        released_orders += 1
                    active_match.status = "Expired"
                    active_match.farmer_accepted = False
                    active_match.vendor_accepted = False
                    db.add(active_match)
                    logger.info("Released order %d due to spoiled lot %d", order.id if order else '?', lot.id)

    if spoiled_count:
        db.commit()
        logger.info(
            "Ultimate rule: spoiled %d lots, released %d orders back to Open.",
            spoiled_count, released_orders,
        )

        for lot_id in spoiled_lot_ids:
            _send_spoiled_lot_sms(db, lot_id)
    else:
        logger.debug("No pending lots have spoiled.")


# ---------------------------------------------------------------------------
# Legacy open lot spoilage expiry – now uses SPOILED status
# ---------------------------------------------------------------------------
def expire_open_lots(db: Optional[Session] = None) -> None:
    if db is None:
        with Session(engine) as session:
            _expire_open_lots_logic(session)
    else:
        _expire_open_lots_logic(db)


def _expire_open_lots_logic(db: Session) -> None:
    now = get_utc_now_naive()
    open_lots = db.exec(
        select(SupplyLot).where(SupplyLot.status == "Open")
    ).all()

    count = 0
    spoiled_lot_ids = []

    for lot in open_lots:
        if lot.spoilage_time and now >= lot.spoilage_time:   # naive vs naive
            lot.status = LotStatus.SPOILED
            lot.spoiled_at = now             # record when this happened
            db.add(lot)
            spoiled_lot_ids.append(lot.id)
            count += 1

            # --- In-app notification for farmer (before commit) ---
            farmer = db.get(User, lot.farmer_id)
            if farmer:
                create_notification(
                    db,
                    farmer.id,
                    f"Lot spoiled: {lot.quantity_kg:.0f}kg {lot.crop_type} has spoiled automatically and has been marked as Spoiled.",
                    NotificationType.DANGER,
                )

            logger.info("Spoiled open lot %d (crop %s) – spoilage time passed", lot.id, lot.crop_type)

    if count:
        db.commit()
        logger.info("Spoiled %d open lots.", count)

        for lot_id in spoiled_lot_ids:
            _send_spoiled_lot_sms(db, lot_id)
    else:
        logger.debug("No open lots to expire.")


def _send_spoiled_lot_sms(db: Session, lot_id: int) -> None:
    """Notify the farmer that their lot has spoiled automatically."""
    try:
        lot = db.get(SupplyLot, lot_id)
        if not lot:
            return

        farmer = db.get(User, lot.farmer_id)
        if farmer and farmer.phone:
            farmer_msg = (
                f"[ASPEN] Lot spoiled: {lot.quantity_kg:.0f}kg {lot.crop_type} "
                f"has spoiled automatically and has been marked as Spoiled."
            )
            send_sms(phone=farmer.phone, message=farmer_msg, user_id=farmer.id, db_session=db)
    except Exception:
        logger.exception("Failed to send spoiled lot SMS for lot %d", lot_id)