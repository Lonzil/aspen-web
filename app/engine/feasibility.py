from __future__ import annotations

from datetime import datetime
from typing import List, Tuple

from .geo import haversine_distance_km, travel_time_h
from .models import SupplyLot, DemandOrder, EngineConfig


def remaining_shelf_life_h(spoilage_time: datetime,
                           current_time: datetime,
                           dispatch_buffer_h: float) -> float:
    delta_h = (spoilage_time - current_time).total_seconds() / 3600.0
    return max(0.0, delta_h - dispatch_buffer_h)


def is_feasible(remaining_life_h: float, travel_time_hours: float,
                min_shelf_life_required_h: float) -> bool:
    return (remaining_life_h - travel_time_hours) >= min_shelf_life_required_h


def build_feasible_pairs(supply_lots: List[SupplyLot],
                         demand_orders: List[DemandOrder],
                         config: EngineConfig,
                         current_time: datetime):
    feasible: List[Tuple[SupplyLot, DemandOrder, float, float]] = []
    supply_has_edge = set()
    demand_has_edge = set()

    for s in supply_lots:
        life_h = remaining_shelf_life_h(s.spoilage_time, current_time,
                                        config.dispatch_buffer_h)
        for d in demand_orders:
            if s.crop_type != d.crop_type:
                continue
            dist_km = haversine_distance_km(s.lat, s.lon, d.lat, d.lon)
            travel_h = travel_time_h(dist_km, config.avg_speed_kmph)
            if is_feasible(life_h, travel_h, d.min_shelf_life_h):
                feasible.append((s, d, dist_km, life_h))
                supply_has_edge.add(s.id)
                demand_has_edge.add(d.id)

    infeasible_supply = [s.id for s in supply_lots if s.id not in supply_has_edge]
    infeasible_demand = [d.id for d in demand_orders if d.id not in demand_has_edge]
    return feasible, infeasible_supply, infeasible_demand


def compute_d_max_l_max(feasible_pairs):
    if not feasible_pairs:
        return 0.001, 0.001
    d_max = max(p[2] for p in feasible_pairs)
    l_max = max(p[3] for p in feasible_pairs)
    return max(d_max, 0.001), max(l_max, 0.001)


def edge_cost(distance_km: float, remaining_life_h: float,
              d_max: float, l_max: float, config: EngineConfig) -> float:
    return (config.w_distance * (distance_km / d_max) +
            config.w_freshness * (remaining_life_h / l_max))