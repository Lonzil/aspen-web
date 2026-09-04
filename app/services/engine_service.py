"""
Engine Integration Service
--------------------------
Bridge between the web application (database) and the core matching engine.

This module is owned by Person A (the engine lead).  It reads open supply
lots and demand orders from the database, converts them to the engine's
dataclasses, runs `run_matching_engine`, persists the results, and
collects SMS notifications that will be sent as FastAPI BackgroundTasks.

Thread‑safety: a `threading.Lock` prevents concurrent engine runs.
"""

import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from sqlmodel import Session, select

from app.config import ACCEPTANCE_WINDOW_MINUTES, get_engine_config
from app.engine.models import SupplyLot as EngineSupplyLot
from app.engine.models import DemandOrder as EngineDemandOrder
from app.engine.orchestrator import run_matching_engine

from app.models import (
    DemandOrder,
    EngineRun,
    Match,
    SupplyLot,
    User,
    NotificationType,
    get_utc_now_naive,      # naive UTC for storage
)
from app.services.notification_service import create_notification

logger = logging.getLogger("aspen.engine_service")

# Prevent overlapping runs (manual + scheduled)
_engine_lock = threading.RLock()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def run_engine_service(db: Session) -> Tuple[dict, List[dict]]:
    """
    Run the ASPEN matching engine against all open supply/demand in the DB.

    Parameters
    ----------
    db : Session
        A SQLModel database session.

    Returns
    -------
    summary : dict
        Totals: {"matched_kg", "waste_kg", "unmet_kg", "runtime_ms"}.
        If no open supply or no open demand, a "message" key is included.
    sms_notifications : list[dict]
        Each dict has "phone", "message", and "user_id" keys, to be sent via
        BackgroundTasks or directly by the scheduler.
    """
    if not _engine_lock.acquire(blocking=False):
        logger.warning("Engine is already running – skipping this trigger.")
        return {"matched_kg": 0, "waste_kg": 0, "unmet_kg": 0, "runtime_ms": 0,
                "message": "Engine is already running. Please wait."}, []

    # Start the overall service timer as soon as we have the lock and know we'll run
    service_start = time.perf_counter()

    # These will be assigned inside the try block; we declare them here so the
    # except handler can access them if the error occurs after fetching.
    open_supply: Optional[List[SupplyLot]] = None
    open_demand: Optional[List[DemandOrder]] = None
    current_time = get_utc_now_naive()    # naive UTC – all DB dates are naive

    try:
        # 1. Fetch open lots and orders
        open_supply = db.exec(
            select(SupplyLot).where(SupplyLot.status == "Open")
        ).all()
        open_demand = db.exec(
            select(DemandOrder).where(DemandOrder.status == "Open")
        ).all()

        if not open_supply or not open_demand:
            logger.info("No open supply or demand – skipping engine run.")
            return {"matched_kg": 0, "waste_kg": 0, "unmet_kg": 0, "runtime_ms": 0,
                    "message": "Engine did not run – no open supply or demand to match."}, []

        # --- Compute per‑crop counts for the engine run log ---
        supply_by_crop: dict[str, int] = defaultdict(int)
        for s in open_supply:
            supply_by_crop[s.crop_type] += 1

        demand_by_crop: dict[str, int] = defaultdict(int)
        for d in open_demand:
            demand_by_crop[d.crop_type] += 1

        # 2. Convert to engine dataclasses
        engine_supply = [
            EngineSupplyLot(
                id=f"S{s.id}",
                crop_type=s.crop_type,
                quantity_kg=s.quantity_kg,
                spoilage_time=s.spoilage_time,   # naive from DB – engine can handle it
                lat=s.lat,
                lon=s.lon,
                farmer_id=str(s.farmer_id),
            )
            for s in open_supply
        ]
        engine_demand = [
            EngineDemandOrder(
                id=f"D{d.id}",
                crop_type=d.crop_type,
                quantity_kg=d.quantity_kg,
                min_shelf_life_h=d.min_shelf_life_h,
                lat=d.lat,
                lon=d.lon,
                vendor_id=str(d.vendor_id),
            )
            for d in open_demand
        ]

        # 3. Build config and run
        config = get_engine_config()
        result = run_matching_engine(
            engine_supply, engine_demand, config, current_time=current_time
        )

        # 4. Persist results
        sms_list: List[dict] = []
        total_matched = 0.0
        total_waste = 0.0
        total_unmet = 0.0

        # Track matched quantities per lot/order for partial splitting
        matched_lot_qty = {}   # lot_id -> total kg matched
        matched_order_qty = {} # order_id -> total kg matched

        for crop_name, crop_result in result.crop_results.items():
            # a. Create Match rows
            for m in crop_result.matches:
                supply_id = int(m.supply_id[1:])  # "S123" -> 123
                demand_id = int(m.demand_id[1:])  # "D456" -> 456

                # Compute travel time and arrival freshness
                travel_time_h = m.distance_km / config.avg_speed_kmph

                lot = db.get(SupplyLot, supply_id)
                order = db.get(DemandOrder, demand_id)

                # --- Cap matched quantity to actual lot/order quantities ---
                actual_matched = m.quantity_kg
                if lot and actual_matched > lot.quantity_kg:
                    actual_matched = lot.quantity_kg
                if order and actual_matched > order.quantity_kg:
                    actual_matched = order.quantity_kg

                if lot:
                    remaining_life_h = max(
                        0.0,
                        (lot.spoilage_time - current_time).total_seconds() / 3600.0
                        - config.dispatch_buffer_h,
                    )
                    arrival_freshness_h = max(0.0, remaining_life_h - travel_time_h)
                else:
                    arrival_freshness_h = 0.0

                match = Match(
                    supply_lot_id=supply_id,
                    demand_order_id=demand_id,
                    quantity_kg=actual_matched,
                    distance_km=m.distance_km,
                    travel_time_h=travel_time_h,
                    arrival_freshness_h=arrival_freshness_h,
                    optimisation_cost=m.cost,
                    priority_score=m.priority_score,
                    status="Matched",
                    created_at=current_time,   # naive UTC
                    accept_deadline=current_time + timedelta(minutes=ACCEPTANCE_WINDOW_MINUTES),
                )
                db.add(match)
                total_matched += actual_matched

                # Track matched quantities
                matched_lot_qty[supply_id] = matched_lot_qty.get(supply_id, 0) + actual_matched
                matched_order_qty[demand_id] = matched_order_qty.get(demand_id, 0) + actual_matched

                # Update lot/order status
                _set_matched(db, supply_id, demand_id)

                # Build SMS with distance included (now includes user_id)
                sms_list.extend(
                    _build_match_sms(
                        db,
                        supply_id,
                        demand_id,
                        actual_matched,
                        crop_name,
                        m.distance_km,
                    )
                )

                # --- In-app notifications for the new match ---
                farmer = db.get(User, lot.farmer_id)
                vendor = db.get(User, order.vendor_id)

                if farmer:
                    create_notification(
                        db,
                        farmer.id,
                        f"New match: {actual_matched:.0f}kg {crop_name} with {vendor.name if vendor else 'Vendor'}.",
                        NotificationType.INFO,
                    )
                if vendor:
                    create_notification(
                        db,
                        vendor.id,
                        f"New match: {actual_matched:.0f}kg {crop_name} from {farmer.name if farmer else 'Farmer'}.",
                        NotificationType.INFO,
                    )

            # b. Waste & unmet
            total_waste += crop_result.waste_kg
            total_unmet += crop_result.unmet_kg

        # 5. Split partially matched lots and orders (rounded to 1 decimal)
        for lot in open_supply:
            lot_id = lot.id
            if lot_id in matched_lot_qty:
                matched_kg = matched_lot_qty[lot_id]
                if matched_kg < lot.quantity_kg:
                    remainder = round(lot.quantity_kg - matched_kg, 1)
                    lot.quantity_kg = matched_kg
                    db.add(lot)
                    new_lot = SupplyLot(
                        farmer_id=lot.farmer_id,
                        crop_type=lot.crop_type,
                        quantity_kg=remainder,
                        spoilage_time=lot.spoilage_time,
                        lat=lot.lat,
                        lon=lot.lon,
                        location_label=lot.location_label,
                        status="Open",
                    )
                    db.add(new_lot)

        for order in open_demand:
            order_id = order.id
            if order_id in matched_order_qty:
                matched_kg = matched_order_qty[order_id]
                if matched_kg < order.quantity_kg:
                    remainder = round(order.quantity_kg - matched_kg, 1)
                    order.quantity_kg = matched_kg
                    db.add(order)
                    new_order = DemandOrder(
                        vendor_id=order.vendor_id,
                        crop_type=order.crop_type,
                        quantity_kg=remainder,
                        min_shelf_life_h=order.min_shelf_life_h,
                        lat=order.lat,
                        lon=order.lon,
                        location_label=order.location_label,
                        notes=order.notes,          # ← preserve vendor handling instructions
                        status="Open",
                    )
                    db.add(new_order)

        # 6. Capture total service time BEFORE committing (to include DB writes)
        total_ms = (time.perf_counter() - service_start) * 1000

        # 7. Create EngineRun records with the correct total service time
        for crop_name, crop_result in result.crop_results.items():
            run = EngineRun(
                crop_type=crop_name,
                supply_count=supply_by_crop.get(crop_name, 0),
                demand_count=demand_by_crop.get(crop_name, 0),
                matched_kg=sum(m.quantity_kg for m in crop_result.matches),
                waste_kg=crop_result.waste_kg,
                unmet_kg=crop_result.unmet_kg,
                runtime_ms=total_ms,
                status="success",
                created_at=current_time,
            )
            db.add(run)

        # 8. Commit everything
        db.commit()

        summary = {
            "matched_kg": total_matched,
            "waste_kg": total_waste,
            "unmet_kg": total_unmet,
            "runtime_ms": total_ms,
        }
        logger.info("Engine run complete: %s", summary)
        return summary, sms_list

    except Exception:
        logger.exception("Engine run failed")

        # Rollback any partial changes (matches, lot/order status updates)
        db.rollback()

        # Compute runtime up to this point
        fail_ms = (time.perf_counter() - service_start) * 1000

        # If we managed to fetch open lots/orders, create per‑crop failure rows
        if open_supply is not None and open_demand is not None:
            supply_by_crop_fail: dict[str, int] = defaultdict(int)
            for s in open_supply:
                supply_by_crop_fail[s.crop_type] += 1
            demand_by_crop_fail: dict[str, int] = defaultdict(int)
            for d in open_demand:
                demand_by_crop_fail[d.crop_type] += 1

            all_crops = set(supply_by_crop_fail.keys()) | set(demand_by_crop_fail.keys())
            for crop in sorted(all_crops):
                run = EngineRun(
                    crop_type=crop,
                    supply_count=supply_by_crop_fail.get(crop, 0),
                    demand_count=demand_by_crop_fail.get(crop, 0),
                    matched_kg=0.0,
                    waste_kg=0.0,
                    unmet_kg=0.0,
                    runtime_ms=fail_ms,
                    status="failed",
                    created_at=current_time,
                )
                db.add(run)
        else:
            # Error occurred before fetching – create a generic failure row
            run = EngineRun(
                crop_type=None,
                supply_count=0,
                demand_count=0,
                matched_kg=0.0,
                waste_kg=0.0,
                unmet_kg=0.0,
                runtime_ms=fail_ms,
                status="failed",
                created_at=current_time,
            )
            db.add(run)

        # --- Create in-app notifications for admins about the failure ---
        try:
            admins = db.exec(
                select(User).where(User.role == "admin", User.is_active == True)
            ).all()
            for admin in admins:
                create_notification(
                    db,
                    admin.id,
                    f"Engine run failed after {fail_ms/1000:.1f}s. Please check engine logs.",
                    NotificationType.DANGER,
                )
        except Exception:
            logger.exception("Failed to create admin notifications for engine failure")

        # Commit the failure record(s) and notifications
        db.commit()

        # Build admin SMS notifications for the failure (now includes user_id)
        admin_sms_list = _build_admin_failure_sms(db, fail_ms)

        return {"matched_kg": 0, "waste_kg": 0, "unmet_kg": 0, "runtime_ms": fail_ms,
                "message": "Engine run failed due to an internal error."}, admin_sms_list
    finally:
        _engine_lock.release()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _set_matched(db: Session, supply_id: int, demand_id: int) -> None:
    """Mark the given lot and order as 'Matched'."""
    lot = db.get(SupplyLot, supply_id)
    if lot and lot.status == "Open":
        lot.status = "Matched"
    order = db.get(DemandOrder, demand_id)
    if order and order.status == "Open":
        order.status = "Matched"
    db.add(lot)
    db.add(order)


def _build_match_sms(
    db: Session,
    supply_id: int,
    demand_id: int,
    qty: float,
    crop: str,
    distance_km: float,
) -> List[dict]:
    """Create SMS notification dicts for farmer and vendor, including distance."""
    messages = []
    lot = db.get(SupplyLot, supply_id)
    order = db.get(DemandOrder, demand_id)
    if not lot or not order:
        return messages

    farmer = db.get(User, lot.farmer_id)
    vendor = db.get(User, order.vendor_id)

    if farmer and farmer.phone:
        messages.append({
            "phone": farmer.phone,
            "message": (
                f"[ASPEN] Match: {qty:.0f}kg {crop} -> "
                f"{vendor.name if vendor else 'Vendor'} ({order.id}). "
                f"Distance: {distance_km:.1f} km. "
                f"Contact: {vendor.phone if vendor else ''}"
            ),
            "user_id": farmer.id,  # ✅ include user_id for SMS log association
        })
    if vendor and vendor.phone:
        messages.append({
            "phone": vendor.phone,
            "message": (
                f"[ASPEN] Match: {qty:.0f}kg {crop} from "
                f"{farmer.name if farmer else 'Farmer'} ({lot.id}). "
                f"Distance: {distance_km:.1f} km. "
                f"Contact: {farmer.phone if farmer else ''}"
            ),
            "user_id": vendor.id,  # ✅ include user_id for SMS log association
        })
    return messages


def _build_admin_failure_sms(db: Session, fail_ms: float) -> List[dict]:
    """
    Build SMS notification dicts for all active admins when an engine run fails.

    This is best-effort: if the query fails for any reason, return an empty list
    so the original engine failure is not masked.
    """
    try:
        admins = db.exec(
            select(User).where(User.role == "admin", User.is_active == True)
        ).all()

        messages = []
        for admin in admins:
            if admin.phone:
                messages.append({
                    "phone": admin.phone,
                    "message": (
                        f"[ASPEN] Engine run failed after {fail_ms/1000:.1f}s. "
                        f"Please check engine logs."
                    ),
                    "user_id": admin.id,  # ✅ include user_id for SMS log association
                })
        return messages
    except Exception:
        logger.exception("Failed to build admin SMS notifications for engine failure")
        return []