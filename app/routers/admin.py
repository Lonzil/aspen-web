"""
Admin Router — serves both HTML pages and API endpoints under /admin.   

All GET routes check the Accept header:
  - text/html → return a Jinja2 HTML page (includes flash_messages & config)
  - otherwise  → return JSON (API)
"""

import csv
import io
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import joinedload
from sqlmodel import Session, select, func

from app.config import DEMO_MODE, AVG_SPEED_KMPH, DISPATCH_BUFFER_HOURS, ENGINE_INTERVAL_MINUTES
from app.database import get_session
from app.models import (
    User, SupplyLot, DemandOrder, Match, SmsLog, EngineRun,
    LotStatus, OrderStatus, NotificationType, get_utc_now_naive,
)
from app.services.auth import decode_access_token
from app.services.engine_service import run_engine_service, _engine_lock   # thread‑safe state changes
from app.services.sms import send_sms, send_sms_background
from app.services.notification_service import create_notification
from app.services.benchmark_service import run_benchmark as run_benchmark_service
from app.services.validation import validate_csv_size, validate_csv_row_count

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")


# ---------------------------------------------------------------------------
# Authentication – tries cookie first, then Bearer token
# ---------------------------------------------------------------------------
def require_admin(request: Request, db: Session = Depends(get_session)) -> User:
    # 1. Check cookie (web login)
    token = request.cookies.get("access_token")
    if token:
        try:
            payload = decode_access_token(token)
            user_id = int(payload["sub"])
            user = db.get(User, user_id)
            if user and user.is_active and user.role == "admin":
                return user
        except Exception:
            pass

    # 2. Fallback: Bearer token (API calls)
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        try:
            bearer_token = auth_header.split(" ", 1)[1]
            payload = decode_access_token(bearer_token)
            user_id = int(payload["sub"])
            user = db.get(User, user_id)
            if user and user.is_active and user.role == "admin":
                return user
        except Exception:
            pass

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admins only")


# ---------------------------------------------------------------------------
# Stats helper – updated for multi‑phase workflow
# ---------------------------------------------------------------------------
def get_admin_stats(db: Session):
    # Active supply: all lots that are not yet terminal (not Closed, Spoiled, Expired)
    active_statuses = [
        LotStatus.OPEN,
        LotStatus.MATCHED,
        LotStatus.CONFIRMED,
        LotStatus.IN_TRANSIT,
        LotStatus.DISPUTED,
    ]
    total_listed = db.exec(
        select(func.sum(SupplyLot.quantity_kg))
        .where(SupplyLot.status.in_(active_statuses))
    ).one_or_none() or 0.0

    # Engine-run metrics (from latest runs)
    subquery = (
        select(
            EngineRun.crop_type,
            func.max(EngineRun.created_at).label("max_created")
        )
        .group_by(EngineRun.crop_type)
        .subquery()
    )
    latest_runs = db.exec(
        select(EngineRun)
        .join(
            subquery,
            (EngineRun.crop_type == subquery.c.crop_type) &
            (EngineRun.created_at == subquery.c.max_created)
        )
    ).all()

    total_matched = sum(r.matched_kg for r in latest_runs)
    total_spoiled_engine = sum(r.waste_kg for r in latest_runs)   # from engine waste
    total_unmet = sum(r.unmet_kg for r in latest_runs)

    # System‑wide spoilage (lots that ended as SPOILED)
    total_spoiled_lots = db.exec(
        select(func.sum(SupplyLot.quantity_kg))
        .where(SupplyLot.status == LotStatus.SPOILED)
    ).one_or_none() or 0.0

    # Disputed orders count
    disputed_count = db.exec(
        select(func.count(DemandOrder.id))
        .where(DemandOrder.status == OrderStatus.DISPUTED)
    ).one_or_none() or 0

    # Flagged vendors count
    flagged_vendors_count = db.exec(
        select(func.count(User.id))
        .where(User.role == "vendor", User.flagged == True)
    ).one_or_none() or 0

    # Success rate: all-time Closed kg / all-time listed kg
    all_time_closed = db.exec(
        select(func.sum(Match.quantity_kg)).where(Match.status == "Closed")
    ).one_or_none() or 0.0
    all_time_listed = db.exec(
        select(func.sum(SupplyLot.quantity_kg))
    ).one_or_none() or 0.0
    success_rate = (all_time_closed / all_time_listed * 100) if all_time_listed > 0 else 0.0

    # Avg Distance, Freshness & Optimisation Cost from most recent batch of matches
    latest_match_time = db.exec(select(func.max(Match.created_at))).one_or_none()
    if latest_match_time:
        avg_distance = db.exec(
            select(func.avg(Match.distance_km))
            .where(Match.created_at == latest_match_time)
        ).one_or_none() or 0.0
        avg_freshness = db.exec(
            select(func.avg(Match.arrival_freshness_h))
            .where(Match.created_at == latest_match_time)
        ).one_or_none() or 0.0
        avg_cost = db.exec(
            select(func.avg(Match.optimisation_cost))
            .where(Match.created_at == latest_match_time)
        ).one_or_none() or 0.0
    else:
        avg_distance = 0.0
        avg_freshness = 0.0
        avg_cost = 0.0

    # Engine health status + last run info
    latest_run = db.exec(select(EngineRun).order_by(EngineRun.created_at.desc())).first()
    if latest_run is None:
        engine_status = "READY"
        last_runtime_str = "--"
        last_run_time_str = "--"
    else:
        if latest_run.status == "success":
            engine_status = "HEALTHY"
        else:
            engine_status = "ERROR"
        last_runtime_str = f"{(latest_run.runtime_ms / 1000):.2f}s"
        last_run_time_str = latest_run.created_at.strftime('%H:%M:%S')

    return {
        "total_listed_kg": round(total_listed, 1),
        "food_saved_kg": round(all_time_closed, 1),                  # all‑time delivered produce
        "total_matched_kg": round(total_matched, 1),
        "total_spoiled_engine_kg": round(total_spoiled_engine, 1),
        "total_spoiled_lots_kg": round(total_spoiled_lots, 1),
        "total_unmet_kg": round(total_unmet, 1),
        "disputed_count": disputed_count,
        "flagged_vendors_count": flagged_vendors_count,
        "success_rate_pct": round(success_rate, 1),
        "avg_distance_km": round(avg_distance, 2),
        "avg_arrival_freshness_h": round(avg_freshness, 2),
        "avg_cost": round(avg_cost, 4),
        "engine_status": engine_status,
        "last_runtime": last_runtime_str,
        "last_run_time": last_run_time_str,
    }


# ---------------------------------------------------------------------------
# Template context helper
# ---------------------------------------------------------------------------
def template_context(request: Request, **kwargs) -> dict:
    return {
        "request": request,
        "flash_messages": [],
        "config": {"DEMO_MODE": DEMO_MODE},
        **kwargs,
    }


# ---------------------------------------------------------------------------
# GET /dashboard — returns HTML or JSON (HTML includes chart data)
# ---------------------------------------------------------------------------
@router.get("/dashboard")
async def dashboard(
    request: Request,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    accept = request.headers.get("accept", "")
    stats = get_admin_stats(db)

    # Recent engine runs (for the table) – show per‑crop rows as before
    runs = db.exec(
        select(EngineRun).order_by(EngineRun.created_at.desc()).limit(10)
    ).all()

    # Chart 1 data: per-crop matched vs unmatched (from the latest runs)
    subquery = (
        select(
            EngineRun.crop_type,
            func.max(EngineRun.created_at).label("max_created")
        )
        .group_by(EngineRun.crop_type)
        .subquery()
    )
    latest_runs_per_crop = db.exec(
        select(EngineRun)
        .join(
            subquery,
            (EngineRun.crop_type == subquery.c.crop_type) &
            (EngineRun.created_at == subquery.c.max_created)
        )
    ).all()
    crop_labels = [r.crop_type for r in latest_runs_per_crop]
    matched_data = [r.matched_kg for r in latest_runs_per_crop]
    spoiled_data = [r.waste_kg for r in latest_runs_per_crop]

    # Chart 2 data: cumulative spoilage‑prevention rate per engine run + "Now" point
    distinct_rows = db.exec(
        select(func.distinct(EngineRun.created_at))
        .order_by(EngineRun.created_at.asc())
    ).all()
    timestamps = [row[0] for row in distinct_rows]

    run_labels = [f"Run {i+1}" for i in range(len(timestamps))]
    success_rates = []
    for ts in timestamps:
        closed_kg = db.exec(
            select(func.sum(Match.quantity_kg))
            .where(Match.status == "Closed", Match.created_at <= ts)
        ).one_or_none() or 0.0
        listed_kg = db.exec(
            select(func.sum(SupplyLot.quantity_kg))
            .where(SupplyLot.created_at <= ts)
        ).one_or_none() or 0.0
        rate = (closed_kg / listed_kg * 100) if listed_kg > 0 else 0.0
        success_rates.append(round(rate, 1))

    # Add current (Now) point
    now = get_utc_now_naive()          # naive UTC – all DB dates are naive
    closed_now = db.exec(
        select(func.sum(Match.quantity_kg))
        .where(Match.status == "Closed")
    ).one_or_none() or 0.0
    listed_now = db.exec(
        select(func.sum(SupplyLot.quantity_kg))
    ).one_or_none() or 0.0
    rate_now = (closed_now / listed_now * 100) if listed_now > 0 else 0.0
    success_rates.append(round(rate_now, 1))
    run_labels.append("Now")

    access_token = request.cookies.get("access_token", "")

    if "text/html" in accept:
        return templates.TemplateResponse("admin/dashboard.html",
            template_context(request, current_user=current_user, stats=stats,
                             recent_runs=runs, access_token=access_token,
                             crop_labels=crop_labels,
                             matched_data=matched_data,
                             spoiled_data=spoiled_data,
                             run_labels=run_labels,
                             success_rates=success_rates))
    return stats


# ---------------------------------------------------------------------------
# POST /run-engine – manual trigger
# ---------------------------------------------------------------------------
@router.post("/run-engine")
def trigger_engine(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    summary, sms_list = run_engine_service(db)
    for item in sms_list:
        background_tasks.add_task(
            send_sms_background,
            item["phone"],
            item["message"],
            item.get("user_id"),          # ✅ include user_id for SMS log association
        )
    return summary


# ---------------------------------------------------------------------------
# POST /run-benchmark – run comparison and return results
# ---------------------------------------------------------------------------
@router.post("/run-benchmark")
def run_benchmark_endpoint(
    current_user: User = Depends(require_admin),
):
    """Run the performance benchmark and return consistent metric keys."""
    return run_benchmark_service()


# ---------------------------------------------------------------------------
# GET /sms-log — HTML or JSON (HTML now includes access_token)
# ---------------------------------------------------------------------------
@router.get("/sms-log")
async def sms_log(
    request: Request,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    accept = request.headers.get("accept", "")
    records = db.exec(select(SmsLog).order_by(SmsLog.created_at.desc())).all()
    if "text/html" in accept:
        access_token = request.cookies.get("access_token", "")
        return templates.TemplateResponse("admin/sms_logs.html",
            template_context(request, current_user=current_user, logs=records,
                             access_token=access_token))
    return [
        {
            "id": r.id,
            "phone": r.phone,
            "message": r.message,
            "status": r.status,
            "created_at": r.created_at,
        }
        for r in records
    ]


# ---------------------------------------------------------------------------
# GET /engine-runs – HTML or JSON
# ---------------------------------------------------------------------------
@router.get("/engine-runs")
def engine_runs(
    request: Request,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    accept = request.headers.get("accept", "")
    runs = db.exec(select(EngineRun).order_by(EngineRun.created_at.desc())).all()

    if "text/html" in accept:
        access_token = request.cookies.get("access_token", "")
        return templates.TemplateResponse("admin/engine_runs.html",
            template_context(request, current_user=current_user, runs=runs,
                             access_token=access_token))

    return [
        {
            "id": r.id,
            "crop_type": r.crop_type,
            "supply_count": r.supply_count,
            "demand_count": r.demand_count,
            "matched_kg": r.matched_kg,
            "waste_kg": r.waste_kg,
            "unmet_kg": r.unmet_kg,
            "runtime_ms": r.runtime_ms,
            "status": r.status,
            "created_at": r.created_at,
        }
        for r in runs
    ]


# ---------------------------------------------------------------------------
# GET /audit/* – read‑only tables (JSON only)
# ---------------------------------------------------------------------------
@router.get("/audit/lots")
def audit_lots(
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    lots = db.exec(select(SupplyLot).order_by(SupplyLot.created_at.desc())).all()
    return [{"id": l.id, "crop_type": l.crop_type, "quantity_kg": l.quantity_kg,
             "status": l.status, "farmer_id": l.farmer_id} for l in lots]


@router.get("/audit/orders")
def audit_orders(
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    orders = db.exec(select(DemandOrder).order_by(DemandOrder.created_at.desc())).all()
    return [{"id": o.id, "crop_type": o.crop_type, "quantity_kg": o.quantity_kg,
             "min_shelf_life_h": o.min_shelf_life_h, "status": o.status,
             "vendor_id": o.vendor_id} for o in orders]


@router.get("/audit/matches")
def audit_matches(
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    matches = db.exec(select(Match).order_by(Match.created_at.desc())).all()
    return [{"id": m.id, "supply_lot_id": m.supply_lot_id,
             "demand_order_id": m.demand_order_id, "quantity_kg": m.quantity_kg,
             "distance_km": m.distance_km, "priority_score": m.priority_score,
             "status": m.status} for m in matches]


# ---------------------------------------------------------------------------
# GET /audit — HTML page with tabs for Supply Lots, Demand Orders, Matches
# ---------------------------------------------------------------------------
@router.get("/audit")
def audit_tables_page(
    request: Request,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    accept = request.headers.get("accept", "")

    lots = db.exec(
        select(SupplyLot)
        .options(joinedload(SupplyLot.farmer))
        .order_by(SupplyLot.created_at.desc())
    ).all()

    orders = db.exec(
        select(DemandOrder)
        .options(joinedload(DemandOrder.vendor))
        .order_by(DemandOrder.created_at.desc())
    ).all()

    matches = db.exec(
        select(Match)
        .options(
            joinedload(Match.supply_lot).joinedload(SupplyLot.farmer),
            joinedload(Match.demand_order).joinedload(DemandOrder.vendor),
        )
        .order_by(Match.created_at.desc())
    ).all()

    if "text/html" in accept:
        return templates.TemplateResponse(
            "admin/audit_tables.html",
            template_context(
                request,
                current_user=current_user,
                lots=lots,
                orders=orders,
                matches=matches,
                access_token=request.cookies.get("access_token", ""),
            ),
        )

    return {
        "supply_lots_count": len(lots),
        "demand_orders_count": len(orders),
        "matches_count": len(matches),
    }


# ---------------------------------------------------------------------------
# Import/Export (HTML & API) – HTML now includes dynamic stats
# ---------------------------------------------------------------------------
@router.get("/import-export")
async def import_export_page(
    request: Request,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    accept = request.headers.get("accept", "")

    total_matches = db.exec(select(func.count(Match.id))).one() or 0
    latest_match = db.exec(select(Match).order_by(Match.created_at.desc())).first()
    last_generated = (
        latest_match.created_at.strftime('%Y-%m-%d %H:%M')
        if latest_match and latest_match.created_at else "Never"
    )

    latest_run = db.exec(select(EngineRun).order_by(EngineRun.created_at.desc())).first()
    if latest_run is None:
        engine_status = "READY"
    elif latest_run.status == "success":
        engine_status = "HEALTHY"
    else:
        engine_status = "ERROR"

    context_data = {
        "current_user": current_user,
        "access_token": request.cookies.get("access_token", ""),
        "total_matches": total_matches,
        "last_generated": last_generated,
        "engine_status": engine_status,
        "avg_speed_kmph": AVG_SPEED_KMPH,
        "dispatch_buffer_hours": DISPATCH_BUFFER_HOURS,
        "engine_interval_minutes": ENGINE_INTERVAL_MINUTES,
    }

    if "text/html" in accept:
        return templates.TemplateResponse("admin/import_export.html",
            template_context(request, **context_data))

    return {
        "message": "Use POST to upload CSV or GET /export-csv to download",
        "total_matches": total_matches,
        "last_generated": last_generated,
        "engine_status": engine_status,
    }


def _validate_import_row(row: dict, row_type: str, row_number: int):
    """Validate a single CSV row. Returns a list of error strings."""
    errors = []

    crop = row.get("crop_type", "").strip().lower()
    if not crop:
        errors.append("Missing crop_type")

    qty_raw = row.get("quantity_kg", "").strip()
    if not qty_raw:
        errors.append("Missing quantity_kg")
    else:
        try:
            qty = float(qty_raw)
            if qty <= 0:
                errors.append("quantity_kg must be positive")
        except ValueError:
            errors.append("Invalid quantity_kg")

    lat_raw = row.get("lat", "").strip()
    lon_raw = row.get("lon", "").strip()
    if not lat_raw or not lon_raw:
        errors.append("Missing GPS coordinates")
    else:
        try:
            lat = float(lat_raw)
            lon = float(lon_raw)
            if not (-90 <= lat <= 90):
                errors.append("Invalid latitude")
            if not (-180 <= lon <= 180):
                errors.append("Invalid longitude")
        except ValueError:
            errors.append("Invalid GPS coordinates")

    if row_type == "supply":
        spoilage_str = row.get("spoilage_time", "").strip()
        if not spoilage_str:
            errors.append("Missing spoilage_time")
        else:
            try:
                if spoilage_str.startswith("+"):
                    time_part = spoilage_str[1:]
                    if time_part.endswith("h"):
                        hours = float(time_part[:-1])
                    else:
                        hours = float(time_part)
                    if hours <= 0:
                        errors.append("spoilage_time must be positive")
                else:
                    datetime.fromisoformat(spoilage_str)
            except Exception:
                errors.append("Invalid spoilage_time")

    elif row_type == "demand":
        min_shelf_raw = row.get("min_shelf_life_h", "").strip()
        if not min_shelf_raw:
            errors.append("Missing min_shelf_life_h")
        else:
            try:
                min_shelf = float(min_shelf_raw)
                if min_shelf <= 0:
                    errors.append("min_shelf_life_h must be positive")
            except ValueError:
                errors.append("Invalid min_shelf_life_h")

    return errors


@router.post("/import-csv")
def import_csv(
    file: UploadFile = File(...),
    user_id: int = Query(...),
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    user = db.get(User, user_id)
    if not user or user.role not in ("farmer", "vendor"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user ID")

    # Read file bytes
    contents_bytes = file.file.read()

    # Validate CSV size
    validate_csv_size(contents_bytes)

    # Decode
    try:
        contents = contents_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="CSV file must be UTF-8 encoded.")

    # Parse CSV
    reader = csv.DictReader(io.StringIO(contents))
    if not reader.fieldnames or "type" not in reader.fieldnames:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="CSV must have a 'type' column (supply/demand)")

    # Convert to list for row count validation and iteration
    rows = list(reader)

    # Validate row count
    validate_csv_row_count(rows, max_rows=1000)

    created_supply, created_demand = 0, 0
    skipped_rows = []

    for row_number, row in enumerate(rows, start=1):
        row_type = row.get("type", "").strip().lower()

        if row_type not in ("supply", "demand"):
            skipped_rows.append({
                "row": row_number,
                "reason": "Invalid or missing type (must be supply or demand)"
            })
            continue

        errors = _validate_import_row(row, row_type, row_number)
        if errors:
            skipped_rows.append({
                "row": row_number,
                "reason": "; ".join(errors)
            })
            continue

        crop = row.get("crop_type", "").strip().lower()
        qty = float(row.get("quantity_kg"))
        lat = float(row.get("lat"))
        lon = float(row.get("lon"))
        label = row.get("location_label", "").strip()

        if row_type == "supply":
            spoilage_str = row.get("spoilage_time", "").strip()
            if spoilage_str.startswith("+"):
                time_part = spoilage_str[1:]
                if time_part.endswith("h"):
                    hours = float(time_part[:-1])
                else:
                    hours = float(time_part)
                spoilage_dt = get_utc_now_naive() + timedelta(hours=hours)
            else:
                spoilage_dt = datetime.fromisoformat(spoilage_str)

            lot = SupplyLot(
                farmer_id=user.id,
                crop_type=crop,
                quantity_kg=qty,
                spoilage_time=spoilage_dt,
                lat=lat,
                lon=lon,
                location_label=label,
            )
            db.add(lot)
            created_supply += 1

        elif row_type == "demand":
            min_shelf = float(row.get("min_shelf_life_h"))
            order = DemandOrder(
                vendor_id=user.id,
                crop_type=crop,
                quantity_kg=qty,
                min_shelf_life_h=min_shelf,
                lat=lat,
                lon=lon,
                location_label=label,
            )
            db.add(order)
            created_demand += 1

    if created_supply == 0 and created_demand == 0:
        return {
            "message": "No valid rows imported.",
            "imported_supply": 0,
            "imported_demand": 0,
            "skipped_rows": skipped_rows,
            "engine_summary": None,
        }

    db.commit()

    summary, sms_list = run_engine_service(db)

    for item in sms_list:
        send_sms(
            phone=item["phone"],
            message=item["message"],
            user_id=item.get("user_id"),   # ✅ include user_id for logs
            db_session=db,
        )

    return {
        "message": f"Imported {created_supply} supply lots and {created_demand} demand orders.",
        "imported_supply": created_supply,
        "imported_demand": created_demand,
        "skipped_rows": skipped_rows,
        "engine_summary": summary,
    }


@router.get("/export-csv")
def export_csv(
    export_type: str = Query("matches", pattern="^(matches|pending)$"),
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    output = io.StringIO()
    writer = csv.writer(output)

    if export_type == "pending":
        writer.writerow([
            "order_id", "crop_type", "quantity_kg", "min_shelf_life_h",
            "location_label", "vendor_name", "vendor_phone", "created_at"
        ])

        pending_orders = db.exec(
            select(DemandOrder)
            .where(DemandOrder.status == OrderStatus.OPEN)
            .order_by(DemandOrder.created_at.desc())
        ).all()

        for order in pending_orders:
            vendor = db.get(User, order.vendor_id)
            writer.writerow([
                order.id,
                order.crop_type,
                order.quantity_kg,
                order.min_shelf_life_h,
                order.location_label or "",
                vendor.name if vendor else "",
                vendor.phone if vendor else "",
                order.created_at.strftime('%Y-%m-%d %H:%M') if order.created_at else "",
            ])

        csv_bytes = output.getvalue().encode("utf-8")
        return StreamingResponse(
            iter([csv_bytes]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=aspen_pending_orders.csv"},
        )

    writer.writerow([
        "match_id", "supply_lot_id", "demand_order_id", "crop_type",
        "quantity_kg", "distance_km", "priority_score", "status",
        "farmer_name", "vendor_name"
    ])

    matches = db.exec(select(Match).order_by(Match.created_at.desc())).all()
    for m in matches:
        supply = db.get(SupplyLot, m.supply_lot_id)
        demand = db.get(DemandOrder, m.demand_order_id)
        farmer = db.get(User, supply.farmer_id) if supply else None
        vendor = db.get(User, demand.vendor_id) if demand else None
        writer.writerow([
            m.id,
            m.supply_lot_id,
            m.demand_order_id,
            supply.crop_type if supply else "",
            m.quantity_kg,
            m.distance_km,
            m.priority_score,
            m.status,
            farmer.name if farmer else "",
            vendor.name if vendor else "",
        ])

    csv_bytes = output.getvalue().encode("utf-8")
    return StreamingResponse(
        iter([csv_bytes]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=aspen_matches.csv"},
    )


# ---------------------------------------------------------------------------
# Phase 4 – Admin Dispute Resolution (HTML + JSON)
# ---------------------------------------------------------------------------
class ResolveDisputeRequest(BaseModel):
    resolution: str = Field(pattern="^(APPROVE|REJECT)$")
    reason: str = Field(min_length=1, max_length=500)


@router.get("/disputes")
async def list_disputes(
    request: Request,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    accept = request.headers.get("accept", "")
    disputed_matches = db.exec(
    select(Match).where(
        Match.status == "Disputed",
        Match.dispute_resolution == None   # only unresolved disputes
    )
    .order_by(Match.created_at.desc())
    ).all()

    dispute_data = []
    for m in disputed_matches:
        lot = db.get(SupplyLot, m.supply_lot_id)
        order = db.get(DemandOrder, m.demand_order_id)
        vendor = db.get(User, order.vendor_id) if order else None
        farmer = db.get(User, lot.farmer_id) if lot else None

        dispute_data.append({
            "match_id": m.id,
            "crop_type": lot.crop_type if lot else "Unknown",
            "quantity_kg": m.quantity_kg,
            "farmer_name": farmer.name if farmer else "Unknown",
            "vendor_name": vendor.name if vendor else "Unknown",
            "vendor_phone": vendor.phone if vendor else "",
            "vendor_spoilage_claims_count": vendor.spoilage_claims_count if vendor else 0,
            "photo_url": m.photo_url,
            "recorded_shelf_life_at_receipt_h": m.recorded_shelf_life_at_receipt_h,
            "distance_km": m.distance_km,
            "min_shelf_life_required_h": order.min_shelf_life_h if order else None,
            "disputed_at": m.photo_uploaded_at,
            "dispute_resolution": m.dispute_resolution,
        })

    if "text/html" in accept:
        access_token = request.cookies.get("access_token", "")
        return templates.TemplateResponse("admin/disputes.html",
            template_context(request, current_user=current_user, disputes=dispute_data,
                             access_token=access_token))
    return dispute_data


@router.post("/disputes/{match_id}/resolve")
def resolve_dispute(
    match_id: int,
    body: ResolveDisputeRequest,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    with _engine_lock:
        match = db.get(Match, match_id)
        if not match or match.status != "Disputed":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Disputed match not found")

        lot = db.get(SupplyLot, match.supply_lot_id)
        order = db.get(DemandOrder, match.demand_order_id)
        if not lot or not order:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid match data")

        reason = body.reason.strip()

        if body.resolution == "APPROVE":
            lot.status = LotStatus.SPOILED
            lot.spoiled_at = get_utc_now_naive()
            order.status = OrderStatus.OPEN
            match.status = "Expired"
            match.dispute_resolution = reason
            message = f"Spoilage claim approved. Reason: {reason}"
        else:
            lot.status = LotStatus.CLOSED
            order.status = OrderStatus.CLOSED
            match.status = "Closed"
            match.closed_at = get_utc_now_naive()
            match.dispute_resolution = reason
            message = f"Spoilage claim rejected. Reason: {reason}"

        db.add(lot)
        db.add(order)
        db.add(match)

        farmer = db.get(User, lot.farmer_id)
        vendor = db.get(User, order.vendor_id)

        if farmer and farmer.phone:
            farmer_msg = (
                f"[ASPEN] Dispute resolved: {message} "
                f"(Match #{match.id}, {match.quantity_kg:.0f}kg {lot.crop_type})."
            )
            send_sms(phone=farmer.phone, message=farmer_msg, user_id=farmer.id, db_session=db)

        if vendor and vendor.phone:
            vendor_msg = (
                f"[ASPEN] Dispute resolved: {message} "
                f"(Match #{match.id}, {match.quantity_kg:.0f}kg {lot.crop_type})."
            )
            send_sms(phone=vendor.phone, message=vendor_msg, user_id=vendor.id, db_session=db)

        if farmer:
            create_notification(
                db,
                farmer.id,
                f"Dispute resolved: {message} (Match #{match.id}, {match.quantity_kg:.0f}kg {lot.crop_type}).",
                NotificationType.INFO,
            )
        if vendor:
            create_notification(
                db,
                vendor.id,
                f"Dispute resolved: {message} (Match #{match.id}, {match.quantity_kg:.0f}kg {lot.crop_type}).",
                NotificationType.INFO,
            )

        db.commit()
        return {"message": message}


# ---------------------------------------------------------------------------
# Phase 5 – Trust Score / Flagged Vendors (HTML + JSON)
# ---------------------------------------------------------------------------
@router.get("/flagged-vendors")
async def list_flagged_vendors(
    request: Request,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    accept = request.headers.get("accept", "")
    flagged = db.exec(
        select(User).where(User.role == "vendor", User.flagged == True)
    ).all()

    flagged_data = []
    for vendor in flagged:
        flagged_data.append({
            "id": vendor.id,
            "name": vendor.name,
            "phone": vendor.phone,
            "spoilage_claims_count": vendor.spoilage_claims_count,
            "flag_reason": vendor.flag_reason,
        })

    if "text/html" in accept:
        access_token = request.cookies.get("access_token", "")
        return templates.TemplateResponse("admin/flagged_vendors.html",
            template_context(request, current_user=current_user, flagged=flagged_data,
                             access_token=access_token))
    return flagged_data


@router.post("/flagged-vendors/{user_id}/unflag")
def unflag_vendor(
    user_id: int,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    vendor = db.get(User, user_id)
    if not vendor or vendor.role != "vendor":
        raise HTTPException(status_code=404, detail="Vendor not found")

    vendor.flagged = False
    vendor.flag_reason = None
    db.add(vendor)
    db.commit()
    return {"message": f"Vendor {vendor.name} has been un‑flagged."}