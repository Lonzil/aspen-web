from dataclasses import dataclass
from datetime import datetime


@dataclass
class SupplyLot:
    id: str
    crop_type: str
    quantity_kg: float
    spoilage_time: datetime
    lat: float
    lon: float
    farmer_id: str = ""


@dataclass
class DemandOrder:
    id: str
    crop_type: str
    quantity_kg: float
    min_shelf_life_h: float   # required remaining shelf life after delivery
    lat: float
    lon: float
    vendor_id: str = ""


@dataclass
class MatchEdge:
    supply_id: str
    demand_id: str
    quantity_kg: float
    distance_km: float
    cost: float            # real cost (0‑1)
    priority_score: int    # 0‑100, display only


@dataclass
class EngineConfig:
    dispatch_buffer_h: float = 1.5
    avg_speed_kmph: float = 30.0
    w_distance: float = 0.6
    w_freshness: float = 0.4
    global_max_distance_km: float = 100.0
    global_max_shelf_life_h: float = 48.0

    def __post_init__(self):
        if abs((self.w_distance + self.w_freshness) - 1.0) > 1e-9:
            raise ValueError("w_distance + w_freshness must equal 1.0")
        if self.dispatch_buffer_h < 0:
            raise ValueError("dispatch_buffer_h must be >= 0")
        if self.avg_speed_kmph <= 0:
            raise ValueError("avg_speed_kmph must be > 0")


class CropResult:
    def __init__(self, crop_type, matches, waste_kg, unmet_kg,
                 infeasible_supply_ids, infeasible_demand_ids):
        self.crop_type = crop_type
        self.matches = matches
        self.waste_kg = waste_kg
        self.unmet_kg = unmet_kg
        self.infeasible_supply_ids = infeasible_supply_ids
        self.infeasible_demand_ids = infeasible_demand_ids


class EngineResult:
    def __init__(self):
        self.crop_results = {}
        self.runtime_seconds = 0.0

    @property
    def total_matched_kg(self):
        return sum(m.quantity_kg for r in self.crop_results.values() for m in r.matches)

    @property
    def total_waste_kg(self):
        return sum(r.waste_kg for r in self.crop_results.values())

    @property
    def total_unmet_kg(self):
        return sum(r.unmet_kg for r in self.crop_results.values())