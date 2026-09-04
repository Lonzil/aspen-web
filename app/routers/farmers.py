"""
Farmer Router

Endpoints:
    POST   /lots        – multi‑crop batch listing (auto‑split into atomic lots)
    GET    /lots        – list current farmer's own lots
    GET    /lots/{id}   – single lot details (including match info)
    DELETE /lots/{id}   – delete an Open lot that has never been matched
"""

from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from app.database import get_session
from app.models import DemandOrder, Match, SupplyLot, User, get_utc_now_naive
from app.routers.auth import get_current_user
from app.services.geocode import get_coordinates_from_static
from app.services.validation import validate_crop_type, validate_gps

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response Schemas
# ---------------------------------------------------------------------------
class CropLine(BaseModel):
    crop_type: str = Field(min_length=1, max_length=50)
    quantity_kg: float = Field(gt=0, le=1_000_000)
    spoilage_hours: float = Field(gt=0, le=10_000)

    @field_validator('crop_type')
    @classmethod
    def normalise_crop_type(cls, v: str) -> str:
        return validate_crop_type(v)


class AddLotsRequest(BaseModel):
    crops: List[CropLine]
    region: str = Field(min_length=1, max_length=100)
    district: str = Field(min_length=1, max_length=100)
    town: str = Field(min_length=1, max_length=100)
    lat: Optional[float] = None
    lon: Optional[float] = None
    location_label: Optional[str] = None

    @field_validator('lat')
    @classmethod
    def validate_lat_range(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (-90.0 <= v <= 90.0):
            raise ValueError("Latitude must be between -90 and 90.")
        return v

    @field_validator('lon')
    @classmethod
    def validate_lon_range(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (-180.0 <= v <= 180.0):
            raise ValueError("Longitude must be between -180 and 180.")
        return v


class LotResponse(BaseModel):
    id: int
    crop_type: str
    quantity_kg: float
    spoilage_time: datetime
    location_label: Optional[str]
    lat: float
    lon: float
    status: str
    created_at: datetime
    matches: List[dict] = []

    class Config:
        from_attributes = True


class MatchResponse(BaseModel):
    id: int
    demand_order_id: int
    quantity_kg: float
    distance_km: float
    travel_time_h: float
    arrival_freshness_h: float
    optimisation_cost: float
    priority_score: int
    status: str
    vendor_name: Optional[str] = None
    vendor_phone: Optional[str] = None
    vendor_notes: Optional[str] = None
    min_shelf_life_h: Optional[float] = None

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# POST /lots – multi‑crop batch listing
# ---------------------------------------------------------------------------
@router.post("/lots", status_code=status.HTTP_201_CREATED)
async def add_lots(
    body: AddLotsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """Create one SupplyLot per crop line."""
    if current_user.role != "farmer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only farmers can list produce")

    # Use manual label if provided, else construct from Region → District → Town
    label = body.location_label.strip() if body.location_label else f"{body.town}, {body.district}, {body.region}"

    lat = body.lat
    lon = body.lon
    if lat is None or lon is None:
        coords = get_coordinates_from_static(body.region, body.district, body.town)
        if coords:
            lat, lon = coords
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not determine coordinates for this location. Please try again or enable GPS."
            )
    else:
        # Validate GPS coordinates (if both provided)
        validate_gps(lat, lon)

    created = []
    for crop in body.crops:
        spoilage_time = get_utc_now_naive() + timedelta(hours=crop.spoilage_hours)
        lot = SupplyLot(
            farmer_id=current_user.id,
            crop_type=crop.crop_type,   # already normalised by validator
            quantity_kg=crop.quantity_kg,
            spoilage_time=spoilage_time,
            lat=lat,
            lon=lon,
            location_label=label,
        )
        db.add(lot)
        created.append(lot)

    db.commit()
    for lot in created:
        db.refresh(lot)

    return {
        "message": f"{len(created)} lots created",
        "lots": [{"id": lot.id, "crop_type": lot.crop_type, "quantity_kg": lot.quantity_kg} for lot in created],
    }


# ---------------------------------------------------------------------------
# GET /lots – list farmer's own lots
# ---------------------------------------------------------------------------
@router.get("/lots")
def list_lots(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """Return all lots belonging to the authenticated farmer."""
    if current_user.role != "farmer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only farmers can view lots")

    lots = db.exec(
        select(SupplyLot)
        .options(
            selectinload(SupplyLot.matches)
            .selectinload(Match.demand_order)
            .selectinload(DemandOrder.vendor)
        )
        .where(SupplyLot.farmer_id == current_user.id)
        .order_by(SupplyLot.created_at.desc())
    ).all()

    result = []
    for lot in lots:
        match_list = []
        for m in lot.matches:
            demand = m.demand_order
            vendor = demand.vendor if demand else None
            match_list.append({
                "match_id": m.id,
                "demand_order_id": m.demand_order_id,
                "quantity_kg": m.quantity_kg,
                "distance_km": m.distance_km,
                "travel_time_h": m.travel_time_h,
                "arrival_freshness_h": m.arrival_freshness_h,
                "optimisation_cost": m.optimisation_cost,
                "priority_score": m.priority_score,
                "status": m.status,
                "vendor_name": vendor.name if vendor else None,
                "vendor_phone": vendor.phone if vendor else None,
                "vendor_notes": demand.notes if demand else None,
                "min_shelf_life_h": demand.min_shelf_life_h if demand else None,
                "accept_deadline": m.accept_deadline,
                "dispatch_deadline": m.dispatch_deadline,
                "delivery_deadline": m.delivery_deadline,
                "closed_at": m.closed_at,
                "confirmed_at": m.confirmed_at,
                "dispatched_at": m.dispatched_at,
                "dispute_resolution": m.dispute_resolution,
                "recorded_shelf_life_at_receipt_h": m.recorded_shelf_life_at_receipt_h,
                "photo_url": m.photo_url,
                "farmer_accepted": m.farmer_accepted,
                "vendor_accepted": m.vendor_accepted,
            })
        result.append({
            "id": lot.id,
            "crop_type": lot.crop_type,
            "quantity_kg": lot.quantity_kg,
            "spoilage_time": lot.spoilage_time,
            "location_label": lot.location_label,
            "lat": lot.lat,
            "lon": lot.lon,
            "status": lot.status,
            "created_at": lot.created_at,
            "spoiled_at": lot.spoiled_at,
            "matches": match_list,
        })
    return result


# ---------------------------------------------------------------------------
# GET /lots/{id} – single lot with match details
# ---------------------------------------------------------------------------
@router.get("/lots/{lot_id}")
def get_lot(
    lot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    if current_user.role != "farmer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only farmers can view lots")

    lot = db.exec(
        select(SupplyLot)
        .options(
            selectinload(SupplyLot.matches)
            .selectinload(Match.demand_order)
            .selectinload(DemandOrder.vendor)
        )
        .where(SupplyLot.id == lot_id, SupplyLot.farmer_id == current_user.id)
    ).first()

    if not lot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lot not found")

    match_list = []
    for m in lot.matches:
        demand = m.demand_order
        vendor = demand.vendor if demand else None
        match_list.append({
            "match_id": m.id,
            "demand_order_id": m.demand_order_id,
            "quantity_kg": m.quantity_kg,
            "distance_km": m.distance_km,
            "travel_time_h": m.travel_time_h,
            "arrival_freshness_h": m.arrival_freshness_h,
            "optimisation_cost": m.optimisation_cost,
            "priority_score": m.priority_score,
            "status": m.status,
            "vendor_name": vendor.name if vendor else None,
            "vendor_phone": vendor.phone if vendor else None,
            "vendor_notes": demand.notes if demand else None,
            "min_shelf_life_h": demand.min_shelf_life_h if demand else None,
            "accept_deadline": m.accept_deadline,
            "dispatch_deadline": m.dispatch_deadline,
            "delivery_deadline": m.delivery_deadline,
            "closed_at": m.closed_at,
            "confirmed_at": m.confirmed_at,
            "dispatched_at": m.dispatched_at,
            "dispute_resolution": m.dispute_resolution,
            "recorded_shelf_life_at_receipt_h": m.recorded_shelf_life_at_receipt_h,
            "photo_url": m.photo_url,
            "farmer_accepted": m.farmer_accepted,
            "vendor_accepted": m.vendor_accepted,
        })

    return {
        "id": lot.id,
        "crop_type": lot.crop_type,
        "quantity_kg": lot.quantity_kg,
        "spoilage_time": lot.spoilage_time,
        "location_label": lot.location_label,
        "lat": lot.lat,
        "lon": lot.lon,
        "status": lot.status,
        "created_at": lot.created_at,
        "spoiled_at": lot.spoiled_at,
        "matches": match_list,
    }


# ---------------------------------------------------------------------------
# DELETE /lots/{lot_id} – delete an Open lot with no matches
# ---------------------------------------------------------------------------
@router.delete("/lots/{lot_id}", status_code=status.HTTP_200_OK)
def delete_lot(
    lot_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    if current_user.role != "farmer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only farmers can delete lots")

    lot = db.get(SupplyLot, lot_id)
    if not lot or lot.farmer_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lot not found")

    if lot.status != "Open":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Only Open lots can be deleted.")

    existing_matches = db.exec(
        select(Match).where(Match.supply_lot_id == lot_id)
    ).first()
    if existing_matches:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="This lot has been matched before and cannot be deleted.")

    db.delete(lot)
    db.commit()
    return {"message": f"Lot {lot_id} deleted successfully."}