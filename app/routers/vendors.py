"""
Vendor Router

Endpoints:
    POST   /orders        – multi‑crop order placement (auto‑split into atomic orders)
    GET    /orders        – list current vendor's own orders
    GET    /orders/{id}   – single order details (including match info)
    DELETE /orders/{id}   – delete an Open order that has never been matched
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from app.database import get_session
from app.models import DemandOrder, Match, SupplyLot, User
from app.routers.auth import get_current_user
from app.services.geocode import get_coordinates_from_static
from app.services.validation import validate_crop_type, validate_gps

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response Schemas
# ---------------------------------------------------------------------------
class OrderLine(BaseModel):
    crop_type: str = Field(min_length=1, max_length=50)
    quantity_kg: float = Field(gt=0, le=1_000_000)
    min_shelf_life_h: float = Field(gt=0, le=10_000)

    @field_validator('crop_type')
    @classmethod
    def normalise_crop_type(cls, v: str) -> str:
        return validate_crop_type(v)


class PlaceOrderRequest(BaseModel):
    orders: List[OrderLine]
    region: str = Field(min_length=1, max_length=100)
    district: str = Field(min_length=1, max_length=100)
    town: str = Field(min_length=1, max_length=100)
    lat: Optional[float] = None
    lon: Optional[float] = None
    location_label: Optional[str] = None
    notes: Optional[str] = None

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


class OrderResponse(BaseModel):
    id: int
    crop_type: str
    quantity_kg: float
    min_shelf_life_h: float
    location_label: Optional[str]
    lat: float
    lon: float
    status: str
    created_at: datetime
    notes: Optional[str] = None
    matches: List[dict] = []

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# POST /orders – multi‑crop order placement
# ---------------------------------------------------------------------------
@router.post("/orders", status_code=status.HTTP_201_CREATED)
async def place_orders(
    body: PlaceOrderRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """Create one DemandOrder per order line."""
    if current_user.role != "vendor":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only vendors can place orders")

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
        validate_gps(lat, lon)

    created = []
    for item in body.orders:
        order = DemandOrder(
            vendor_id=current_user.id,
            crop_type=item.crop_type,   # already normalised
            quantity_kg=item.quantity_kg,
            min_shelf_life_h=item.min_shelf_life_h,
            lat=lat,
            lon=lon,
            location_label=label,
            notes=body.notes,
        )
        db.add(order)
        created.append(order)

    db.commit()
    for order in created:
        db.refresh(order)

    return {
        "message": f"{len(created)} orders placed",
        "orders": [{"id": o.id, "crop_type": o.crop_type, "quantity_kg": o.quantity_kg,
                    "min_shelf_life_h": o.min_shelf_life_h} for o in created],
    }


# ---------------------------------------------------------------------------
# GET /orders – list vendor's own orders
# ---------------------------------------------------------------------------
@router.get("/orders")
def list_orders(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    if current_user.role != "vendor":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only vendors can view orders")

    orders = db.exec(
        select(DemandOrder)
        .options(
            selectinload(DemandOrder.matches)
            .selectinload(Match.supply_lot)
            .selectinload(SupplyLot.farmer)
        )
        .where(DemandOrder.vendor_id == current_user.id)
        .order_by(DemandOrder.created_at.desc())
    ).all()

    result = []
    for order in orders:
        match_list = []
        for m in order.matches:
            supply = m.supply_lot
            farmer = supply.farmer if supply else None
            match_list.append({
                "match_id": m.id,
                "supply_lot_id": m.supply_lot_id,
                "quantity_kg": m.quantity_kg,
                "distance_km": m.distance_km,
                "travel_time_h": m.travel_time_h,
                "arrival_freshness_h": m.arrival_freshness_h,
                "optimisation_cost": m.optimisation_cost,
                "priority_score": m.priority_score,
                "status": m.status,
                "farmer_name": farmer.name if farmer else None,
                "farmer_phone": farmer.phone if farmer else None,
                "min_shelf_life_h": order.min_shelf_life_h,
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
            "id": order.id,
            "crop_type": order.crop_type,
            "quantity_kg": order.quantity_kg,
            "min_shelf_life_h": order.min_shelf_life_h,
            "location_label": order.location_label,
            "notes": order.notes,
            "lat": order.lat,
            "lon": order.lon,
            "status": order.status,
            "created_at": order.created_at,
            "matches": match_list,
        })
    return result


# ---------------------------------------------------------------------------
# GET /orders/{id} – single order with match details
# ---------------------------------------------------------------------------
@router.get("/orders/{order_id}")
def get_order(
    order_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    if current_user.role != "vendor":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only vendors can view orders")

    order = db.exec(
        select(DemandOrder)
        .options(
            selectinload(DemandOrder.matches)
            .selectinload(Match.supply_lot)
            .selectinload(SupplyLot.farmer)
        )
        .where(DemandOrder.id == order_id, DemandOrder.vendor_id == current_user.id)
    ).first()

    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    match_list = []
    for m in order.matches:
        supply = m.supply_lot
        farmer = supply.farmer if supply else None
        match_list.append({
            "match_id": m.id,
            "supply_lot_id": m.supply_lot_id,
            "quantity_kg": m.quantity_kg,
            "distance_km": m.distance_km,
            "travel_time_h": m.travel_time_h,
            "arrival_freshness_h": m.arrival_freshness_h,
            "optimisation_cost": m.optimisation_cost,
            "priority_score": m.priority_score,
            "status": m.status,
            "farmer_name": farmer.name if farmer else None,
            "farmer_phone": farmer.phone if farmer else None,
            "min_shelf_life_h": order.min_shelf_life_h,
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
        "id": order.id,
        "crop_type": order.crop_type,
        "quantity_kg": order.quantity_kg,
        "min_shelf_life_h": order.min_shelf_life_h,
        "location_label": order.location_label,
        "notes": order.notes,
        "lat": order.lat,
        "lon": order.lon,
        "status": order.status,
        "created_at": order.created_at,
        "matches": match_list,
    }


# ---------------------------------------------------------------------------
# DELETE /orders/{order_id} – delete an Open order with no matches
# ---------------------------------------------------------------------------
@router.delete("/orders/{order_id}", status_code=status.HTTP_200_OK)
def delete_order(
    order_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    if current_user.role != "vendor":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only vendors can delete orders")

    order = db.get(DemandOrder, order_id)
    if not order or order.vendor_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    if order.status != "Open":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Only Open orders can be deleted.")

    existing_matches = db.exec(
        select(Match).where(Match.demand_order_id == order_id)
    ).first()
    if existing_matches:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="This order has been matched before and cannot be deleted.")

    db.delete(order)
    db.commit()
    return {"message": f"Order {order_id} deleted successfully."}