"""
Match Router – Phases 1‑3: Acceptance, Dispatch, Delivery & Disputes

Endpoints:
    POST /matches/{match_id}/accept         – accept a match
    POST /matches/{match_id}/decline        – decline a match
    POST /matches/{match_id}/dispatch       – farmer dispatches
    POST /matches/{match_id}/receive-fresh  – vendor confirms fresh delivery
    POST /matches/{match_id}/report-spoiled – vendor reports spoilage (photo compulsory)
"""

import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlmodel import Session, select

from app.config import (
    ACCEPTANCE_WINDOW_MINUTES,
    DISPATCH_WINDOW_MINUTES,
    AVG_SPEED_KMPH,
    DISPATCH_BUFFER_HOURS,
    DELIVERY_TRAVEL_MULTIPLIER,
    INSPECTION_BUFFER_HOURS,
    MIN_DELIVERY_WINDOW_HOURS,
)
from app.database import get_session
from app.models import (
    DemandOrder,
    Match,
    SupplyLot,
    User,
    LotStatus,
    OrderStatus,
    NotificationType,
    get_utc_now_naive,
)
from app.routers.auth import get_current_user
from app.services.engine_service import _engine_lock   # thread‑safe state changes
from app.services.sms import send_sms
from app.services.notification_service import create_notification
from app.services.validation import validate_file_size, validate_file_extension
from app.engine.feasibility import remaining_shelf_life_h, is_feasible
from app.engine.geo import travel_time_h

router = APIRouter(prefix="/matches", tags=["matches"])

# Directory for dispute photos
DISPUTES_DIR = Path("app/static/disputes")
DISPUTES_DIR.mkdir(parents=True, exist_ok=True)

# Allowed photo extensions
ALLOWED_PHOTO_EXTENSIONS = {'.jpg', '.jpeg', '.png'}


# ---------------------------------------------------------------------------
# Helper: compute raw remaining shelf life (no dispatch buffer subtracted)
# ---------------------------------------------------------------------------
def _compute_recorded_shelf_life(match: Match, db: Session) -> float:
    """
    Returns the raw remaining shelf life (hours) at the current moment.
    This is the actual time left before spoilage, without any dispatch buffer
    subtraction. It is used for admin review of spoilage claims.
    """
    lot = db.get(SupplyLot, match.supply_lot_id)
    if not lot or not lot.spoilage_time:
        return 0.0
    now = get_utc_now_naive()          # naive UTC – spoilage_time is naive
    return max(0.0, (lot.spoilage_time - now).total_seconds() / 3600.0)


# ---------------------------------------------------------------------------
# Math guard – re‑checks feasibility at the time of acceptance
# ---------------------------------------------------------------------------
def _check_feasibility(match: Match, db: Session) -> bool:
    lot = db.get(SupplyLot, match.supply_lot_id)
    order = db.get(DemandOrder, match.demand_order_id)
    if not lot or not order:
        return False
    now = get_utc_now_naive()          # naive UTC – all DB dates are naive
    remaining = remaining_shelf_life_h(lot.spoilage_time, now, DISPATCH_BUFFER_HOURS)
    travel = travel_time_h(match.distance_km, AVG_SPEED_KMPH)
    return is_feasible(remaining, travel, order.min_shelf_life_h)


# ---------------------------------------------------------------------------
# POST /matches/{match_id}/accept
# ---------------------------------------------------------------------------
@router.post("/{match_id}/accept")
def accept_match(
    match_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """The authenticated user (farmer or vendor) accepts the match.
       If both parties accept, the match and its linked lot/order move to CONFIRMED
       and the dispatch deadline is set."""
    with _engine_lock:          # prevent collision with background tasks
        match = db.get(Match, match_id)
        if not match:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found")

        lot = db.get(SupplyLot, match.supply_lot_id)
        order = db.get(DemandOrder, match.demand_order_id)
        if not lot or not order:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid match data")

        is_farmer = (current_user.role == "farmer" and lot.farmer_id == current_user.id)
        is_vendor = (current_user.role == "vendor" and order.vendor_id == current_user.id)
        if not (is_farmer or is_vendor):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not part of this match")

        if match.status != "Matched":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"This match is already in '{match.status}' status")

        if is_farmer:
            if match.farmer_accepted:
                return {"message": "You have already accepted this match.", "status": "already_accepted"}
            match.farmer_accepted = True
            msg = "Farmer accepted."
        else:
            if match.vendor_accepted:
                return {"message": "You have already accepted this match.", "status": "already_accepted"}
            match.vendor_accepted = True
            msg = "Vendor accepted."

        if not _check_feasibility(match, db):
            if is_farmer:
                match.farmer_accepted = False
            else:
                match.vendor_accepted = False
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The produce can no longer arrive with the required freshness. Match cancelled."
            )

        if match.farmer_accepted and match.vendor_accepted:
            now = get_utc_now_naive()
            match.status = "Confirmed"
            match.confirmed_at = now
            match.dispatch_deadline = now + timedelta(minutes=DISPATCH_WINDOW_MINUTES)
            lot.status = LotStatus.CONFIRMED
            order.status = OrderStatus.CONFIRMED
            db.add(lot)
            db.add(order)
            msg += " Both parties accepted – match confirmed."

            farmer = db.get(User, lot.farmer_id)
            vendor = db.get(User, order.vendor_id)

            deadline_str = match.dispatch_deadline.strftime('%Y-%m-%d %H:%M') if match.dispatch_deadline else "N/A"

            if farmer and farmer.phone:
                farmer_msg = (
                    f"[ASPEN] Match confirmed: {match.quantity_kg:.0f}kg {lot.crop_type} with "
                    f"{vendor.name if vendor else 'Vendor'}. "
                    f"Please dispatch by {deadline_str}. Contact: {vendor.phone if vendor else ''}"
                )
                send_sms(phone=farmer.phone, message=farmer_msg, user_id=farmer.id, db_session=db)
                create_notification(
                    db, farmer.id,
                    f"Match confirmed: {match.quantity_kg:.0f}kg {lot.crop_type} with {vendor.name if vendor else 'Vendor'}.",
                    NotificationType.SUCCESS,
                )

            if vendor and vendor.phone:
                vendor_msg = (
                    f"[ASPEN] Match confirmed: {match.quantity_kg:.0f}kg {lot.crop_type} from "
                    f"{farmer.name if farmer else 'Farmer'}. "
                    f"Expect dispatch by {deadline_str}. Contact: {farmer.phone if farmer else ''}"
                )
                send_sms(phone=vendor.phone, message=vendor_msg, user_id=vendor.id, db_session=db)
                create_notification(
                    db, vendor.id,
                    f"Match confirmed: {match.quantity_kg:.0f}kg {lot.crop_type} from {farmer.name if farmer else 'Farmer'}.",
                    NotificationType.SUCCESS,
                )
        else:
            msg += " Waiting for the other party."

        db.add(match)
        db.commit()
        return {"message": msg, "match_status": match.status}


# ---------------------------------------------------------------------------
# POST /matches/{match_id}/decline
# ---------------------------------------------------------------------------
@router.post("/{match_id}/decline")
def decline_match(
    match_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    with _engine_lock:
        match = db.get(Match, match_id)
        if not match:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found")

        lot = db.get(SupplyLot, match.supply_lot_id)
        order = db.get(DemandOrder, match.demand_order_id)
        if not lot or not order:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid match data")

        is_farmer = (current_user.role == "farmer" and lot.farmer_id == current_user.id)
        is_vendor = (current_user.role == "vendor" and order.vendor_id == current_user.id)
        if not (is_farmer or is_vendor):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not part of this match")

        if match.status != "Matched":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Only matches in 'Matched' status can be declined")

        match.status = "Declined"
        match.farmer_accepted = False
        match.vendor_accepted = False
        lot.status = LotStatus.OPEN
        order.status = OrderStatus.OPEN

        db.add(match)
        db.add(lot)
        db.add(order)

        if is_farmer:
            vendor = db.get(User, order.vendor_id)
            if vendor:
                create_notification(db, vendor.id, f"Match declined by farmer: {match.quantity_kg:.0f}kg {lot.crop_type}.", NotificationType.WARNING)
        else:
            farmer = db.get(User, lot.farmer_id)
            if farmer:
                create_notification(db, farmer.id, f"Match declined by vendor: {match.quantity_kg:.0f}kg {lot.crop_type}.", NotificationType.WARNING)

        db.commit()
        return {"message": "Match declined. Lot and order returned to Open."}


# ---------------------------------------------------------------------------
# POST /matches/{match_id}/dispatch
# ---------------------------------------------------------------------------
@router.post("/{match_id}/dispatch")
def dispatch_match(
    match_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    with _engine_lock:
        match = db.get(Match, match_id)
        if not match:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found")

        lot = db.get(SupplyLot, match.supply_lot_id)
        if not lot or lot.farmer_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Only the farmer of this lot can dispatch")

        if match.status != "Confirmed":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Match must be Confirmed before dispatch")

        if match.dispatched_at is not None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="This match has already been dispatched")

        now = get_utc_now_naive()
        match.status = "In Transit"
        match.dispatched_at = now

        travel_h = travel_time_h(match.distance_km, AVG_SPEED_KMPH)
        worst_case_travel_h = travel_h * DELIVERY_TRAVEL_MULTIPLIER
        deadline = now + timedelta(
            hours=max(MIN_DELIVERY_WINDOW_HOURS,
                      worst_case_travel_h + INSPECTION_BUFFER_HOURS)
        )
        match.delivery_deadline = deadline

        order = db.get(DemandOrder, match.demand_order_id)
        if lot:
            lot.status = LotStatus.IN_TRANSIT
            db.add(lot)
        if order:
            order.status = OrderStatus.IN_TRANSIT
            db.add(order)

        db.add(match)

        farmer = db.get(User, lot.farmer_id)
        vendor = db.get(User, order.vendor_id)

        if vendor and vendor.phone:
            deadline_str = match.delivery_deadline.strftime('%Y-%m-%d %H:%M') if match.delivery_deadline else "N/A"
            vendor_msg = (
                f"[ASPEN] Produce dispatched: {match.quantity_kg:.0f}kg {lot.crop_type} from "
                f"{farmer.name if farmer else 'Farmer'}. Expected delivery by {deadline_str}. "
                f"Contact: {farmer.phone if farmer else ''}"
            )
            send_sms(phone=vendor.phone, message=vendor_msg, user_id=vendor.id, db_session=db)

        if vendor:
            create_notification(db, vendor.id, f"Produce dispatched: {match.quantity_kg:.0f}kg {lot.crop_type} from {farmer.name if farmer else 'Farmer'}.", NotificationType.INFO)

        db.commit()
        return {"message": "Produce dispatched. Delivery deadline set.", "delivery_deadline": deadline}


# ---------------------------------------------------------------------------
# POST /matches/{match_id}/receive-fresh
# ---------------------------------------------------------------------------
@router.post("/{match_id}/receive-fresh")
def receive_fresh(
    match_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    with _engine_lock:
        match = db.get(Match, match_id)
        if not match:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found")

        order = db.get(DemandOrder, match.demand_order_id)
        if not order or order.vendor_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Only the vendor of this order can confirm receipt")

        if match.status != "In Transit":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Match must be In Transit to confirm receipt")

        now = get_utc_now_naive()
        recorded = _compute_recorded_shelf_life(match, db)

        match.status = "Closed"
        match.closed_at = now
        match.recorded_shelf_life_at_receipt_h = recorded

        lot = db.get(SupplyLot, match.supply_lot_id)
        if lot:
            lot.status = LotStatus.CLOSED
            db.add(lot)
        order.status = OrderStatus.CLOSED
        db.add(order)
        db.add(match)

        farmer = db.get(User, lot.farmer_id) if lot else None
        vendor = db.get(User, order.vendor_id) if order else None

        if farmer and farmer.phone:
            farmer_msg = (
                f"[ASPEN] Delivery complete: {match.quantity_kg:.0f}kg {lot.crop_type} "
                f"delivered to {vendor.name if vendor else 'Vendor'}. Thank you."
            )
            send_sms(phone=farmer.phone, message=farmer_msg, user_id=farmer.id, db_session=db)

        if farmer:
            create_notification(db, farmer.id, f"Delivery complete: {match.quantity_kg:.0f}kg {lot.crop_type} delivered to {vendor.name if vendor else 'Vendor'}.", NotificationType.SUCCESS)

        db.commit()
        return {"message": "Delivery confirmed as fresh. Transaction closed."}


# ---------------------------------------------------------------------------
# POST /matches/{match_id}/report-spoiled
# ---------------------------------------------------------------------------
@router.post("/{match_id}/report-spoiled")
def report_spoiled(
    match_id: int,
    photo: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """Vendor reports the produce as spoiled. A photo is mandatory;
       the match is moved to DISPUTED for admin review."""
    with _engine_lock:
        match = db.get(Match, match_id)
        if not match:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found")

        order = db.get(DemandOrder, match.demand_order_id)
        if not order or order.vendor_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Only the vendor of this order can report spoilage")

        if match.status != "In Transit":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Match must be In Transit to report spoilage")

        if match.status == "Disputed":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="A dispute has already been filed for this match")

        # Validate photo
        if not photo.content_type or not photo.content_type.startswith("image/"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Only image files (JPEG, PNG) are accepted")

        original_filename = photo.filename or ""
        validate_file_extension(original_filename, ALLOWED_PHOTO_EXTENSIONS)

        # Check file size (this may consume the file stream, so do it before saving)
        validate_file_size(photo)

        now = get_utc_now_naive()
        timestamp = now.strftime("%Y%m%d_%H%M%S")
        filename = f"dispute_{match_id}_{timestamp}{os.path.splitext(original_filename)[1].lower() or '.jpg'}"
        filepath = DISPUTES_DIR / filename
        with open(filepath, "wb") as buffer:
            shutil.copyfileobj(photo.file, buffer)

        recorded = _compute_recorded_shelf_life(match, db)

        match.status = "Disputed"
        match.photo_url = f"/static/disputes/{filename}"
        match.photo_uploaded_at = now
        match.recorded_shelf_life_at_receipt_h = recorded

        lot = db.get(SupplyLot, match.supply_lot_id)
        if lot:
            lot.status = LotStatus.DISPUTED
            db.add(lot)
        order.status = OrderStatus.DISPUTED
        db.add(order)
        db.add(match)

        vendor = db.get(User, current_user.id)
        if vendor:
            vendor.spoilage_claims_count += 1
            db.add(vendor)

        farmer = db.get(User, lot.farmer_id) if lot else None
        vendor = db.get(User, order.vendor_id) if order else None

        if farmer and farmer.phone:
            farmer_msg = (
                f"[ASPEN] Spoilage claim filed: {match.quantity_kg:.0f}kg {lot.crop_type} "
                f"by {vendor.name if vendor else 'Vendor'}. Admin will review shortly."
            )
            send_sms(phone=farmer.phone, message=farmer_msg, user_id=farmer.id, db_session=db)

        if farmer:
            create_notification(db, farmer.id, f"Spoilage claim filed: {match.quantity_kg:.0f}kg {lot.crop_type} by {vendor.name if vendor else 'Vendor'}.", NotificationType.WARNING)

        admins = db.exec(select(User).where(User.role == "admin", User.is_active == True)).all()
        for admin in admins:
            if admin.phone:
                admin_msg = (
                    f"[ASPEN] New dispute: Match #{match.id} – {match.quantity_kg:.0f}kg "
                    f"{lot.crop_type if lot else 'produce'} "
                    f"({vendor.name if vendor else 'Vendor'} vs {farmer.name if farmer else 'Farmer'}). "
                    f"Please review."
                )
                send_sms(phone=admin.phone, message=admin_msg, user_id=admin.id, db_session=db)

            create_notification(db, admin.id, f"New dispute: Match #{match.id} – {match.quantity_kg:.0f}kg {lot.crop_type if lot else 'produce'}.", NotificationType.WARNING)

        db.commit()
        return {
            "message": "Spoilage reported. Admin will review the dispute.",
            "recorded_shelf_life_h": recorded,
        }