from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import List, Optional

from sqlmodel import Field, Relationship, SQLModel


def get_utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class LotStatus:
    OPEN = "Open"
    MATCHED = "Matched"
    CONFIRMED = "Confirmed"
    IN_TRANSIT = "In Transit"
    CLOSED = "Closed"
    SPOILED = "Spoiled"
    DISPUTED = "Disputed"


class OrderStatus:
    OPEN = "Open"
    MATCHED = "Matched"
    CONFIRMED = "Confirmed"
    IN_TRANSIT = "In Transit"
    CLOSED = "Closed"
    DISPUTED = "Disputed"


class NotificationType(str, enum.Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    DANGER = "danger"


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    phone: str = Field(unique=True, index=True)
    password_hash: str
    role: str = Field(index=True)
    town: str
    region: str
    district: Optional[str] = None
    email: Optional[str] = Field(default=None, unique=True, index=True)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=get_utc_now_naive)

    accepted_terms_at: Optional[datetime] = Field(default=None)
    spoilage_claims_count: int = Field(default=0)
    flagged: bool = Field(default=False)
    flag_reason: Optional[str] = Field(default=None)

    failed_login_attempts: int = Field(default=0)
    last_failed_login_at: Optional[datetime] = Field(default=None)
    locked_until: Optional[datetime] = Field(default=None)

    supply_lots: List["SupplyLot"] = Relationship(back_populates="farmer")
    demand_orders: List["DemandOrder"] = Relationship(back_populates="vendor")
    sms_logs: List["SmsLog"] = Relationship(back_populates="user")
    verification_codes: List["VerificationCode"] = Relationship(back_populates="user")
    password_reset_tokens: List["PasswordResetToken"] = Relationship(back_populates="user")
    notifications: List["Notification"] = Relationship(back_populates="user")


class VerificationCode(SQLModel, table=True):
    __tablename__ = "verification_codes"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    code: str
    purpose: str = "signup"
    expires_at: datetime
    used: bool = Field(default=False)
    created_at: datetime = Field(default_factory=get_utc_now_naive)

    attempts: int = Field(default=0)
    last_attempt_at: Optional[datetime] = Field(default=None)
    resend_count: int = Field(default=0)
    last_resend_at: Optional[datetime] = Field(default=None)

    user: User = Relationship(back_populates="verification_codes")


class PasswordResetToken(SQLModel, table=True):
    __tablename__ = "password_reset_tokens"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    token: str
    expires_at: datetime
    used: bool = Field(default=False)
    created_at: datetime = Field(default_factory=get_utc_now_naive)

    attempts: int = Field(default=0)
    last_attempt_at: Optional[datetime] = Field(default=None)
    resend_count: int = Field(default=0)
    last_resend_at: Optional[datetime] = Field(default=None)

    user: User = Relationship(back_populates="password_reset_tokens")


class SupplyLot(SQLModel, table=True):
    __tablename__ = "supply_lots"

    id: Optional[int] = Field(default=None, primary_key=True)
    farmer_id: int = Field(foreign_key="users.id", index=True)
    crop_type: str = Field(index=True)
    quantity_kg: float
    spoilage_time: datetime
    lat: float
    lon: float
    location_label: Optional[str] = None
    status: str = Field(default=LotStatus.OPEN, index=True)
    created_at: datetime = Field(default_factory=get_utc_now_naive)
    spoiled_at: Optional[datetime] = Field(default=None)

    farmer: User = Relationship(back_populates="supply_lots")
    matches: List["Match"] = Relationship(back_populates="supply_lot")


class DemandOrder(SQLModel, table=True):
    __tablename__ = "demand_orders"

    id: Optional[int] = Field(default=None, primary_key=True)
    vendor_id: int = Field(foreign_key="users.id", index=True)
    crop_type: str = Field(index=True)
    quantity_kg: float
    min_shelf_life_h: float
    lat: float
    lon: float
    location_label: Optional[str] = None
    notes: Optional[str] = None
    status: str = Field(default=OrderStatus.OPEN, index=True)
    created_at: datetime = Field(default_factory=get_utc_now_naive)

    vendor: User = Relationship(back_populates="demand_orders")
    matches: List["Match"] = Relationship(back_populates="demand_order")


class Match(SQLModel, table=True):
    __tablename__ = "matches"

    id: Optional[int] = Field(default=None, primary_key=True)
    supply_lot_id: int = Field(foreign_key="supply_lots.id", index=True)
    demand_order_id: int = Field(foreign_key="demand_orders.id", index=True)
    quantity_kg: float
    distance_km: float
    travel_time_h: float
    arrival_freshness_h: float
    optimisation_cost: float
    priority_score: int
    status: str = Field(default="Matched", index=True)
    created_at: datetime = Field(default_factory=get_utc_now_naive)
    closed_at: Optional[datetime] = Field(default=None)

    farmer_accepted: bool = Field(default=False)
    vendor_accepted: bool = Field(default=False)

    matched_at: datetime = Field(default_factory=get_utc_now_naive)
    confirmed_at: Optional[datetime] = Field(default=None)
    dispatched_at: Optional[datetime] = Field(default=None)

    accept_deadline: Optional[datetime] = Field(default=None)
    dispatch_deadline: Optional[datetime] = Field(default=None)
    delivery_deadline: Optional[datetime] = Field(default=None)

    photo_url: Optional[str] = Field(default=None)
    photo_uploaded_at: Optional[datetime] = Field(default=None)
    recorded_shelf_life_at_receipt_h: Optional[float] = Field(default=None)
    dispute_resolution: Optional[str] = Field(default=None)

    supply_lot: SupplyLot = Relationship(back_populates="matches")
    demand_order: DemandOrder = Relationship(back_populates="matches")


class SmsLog(SQLModel, table=True):
    __tablename__ = "sms_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    phone: str
    message: str
    status: str = Field(index=True)
    created_at: datetime = Field(default_factory=get_utc_now_naive)

    user: Optional[User] = Relationship(back_populates="sms_logs")


class Notification(SQLModel, table=True):
    __tablename__ = "notifications"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    message: str
    type: NotificationType = Field(default=NotificationType.INFO)
    is_read: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=get_utc_now_naive)

    user: User = Relationship(back_populates="notifications")


class EngineRun(SQLModel, table=True):
    __tablename__ = "engine_runs"

    id: Optional[int] = Field(default=None, primary_key=True)
    crop_type: Optional[str] = None
    supply_count: int
    demand_count: int
    matched_kg: float
    waste_kg: float
    unmet_kg: float
    runtime_ms: float
    status: str = Field(default="success", index=True)
    created_at: datetime = Field(default_factory=get_utc_now_naive)