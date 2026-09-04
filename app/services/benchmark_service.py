"""
Benchmark Service
==================
Compares Random Assignment, Greedy Nearest Vendor, and ASPEN
(Minimum‑Cost Flow) on synthetic supply/demand data.

The returned keys match the admin dashboard metric names:
  - matched_kg        -> Matched Supply
  - waste_kg          -> Engine Waste
  - unmet_kg          -> Unmet Demand
  - avg_distance_km   -> Avg Distance
  - runtime_seconds   -> Runtime (s)
"""

import random
import time
from datetime import datetime, timedelta

from app.engine.models import SupplyLot, DemandOrder, EngineConfig
from app.engine.orchestrator import run_matching_engine
from app.engine.geo import haversine_distance_km, travel_time_h
from app.engine.feasibility import remaining_shelf_life_h, is_feasible

CURRENT_TIME = datetime(2026, 8, 15, 12, 0, 0)
CONFIG = EngineConfig(
    dispatch_buffer_h=1.5,
    avg_speed_kmph=30.0,
    w_distance=0.6,
    w_freshness=0.4,
    global_max_distance_km=200.0,
    global_max_shelf_life_h=72.0,
)

NUM_SUPPLY = 120
NUM_DEMAND = 60
SEED = 42


def _generate_data():
    random.seed(SEED)
    supply = []
    demand = []

    for i in range(1, NUM_SUPPLY + 1):
        lat = 5.55 + random.uniform(-0.2, 0.3)
        lon = -0.20 + random.uniform(-0.3, 0.3)
        qty = round(random.uniform(10, 50), 1)
        spoilage_hours = random.uniform(5, 48)
        supply.append(
            SupplyLot(
                id=f"S{i}",
                crop_type="tomato",
                quantity_kg=qty,
                spoilage_time=CURRENT_TIME + timedelta(hours=spoilage_hours),
                lat=lat,
                lon=lon,
                farmer_id=f"F{i}",
            )
        )

    for j in range(1, NUM_DEMAND + 1):
        lat = 5.55 + random.uniform(-0.2, 0.3)
        lon = -0.20 + random.uniform(-0.3, 0.3)
        qty = round(random.uniform(10, 40), 1)
        min_shelf = random.uniform(2, 12)
        demand.append(
            DemandOrder(
                id=f"D{j}",
                crop_type="tomato",
                quantity_kg=qty,
                min_shelf_life_h=min_shelf,
                lat=lat,
                lon=lon,
                vendor_id=f"V{j}",
            )
        )

    return supply, demand


def _get_feasible_pairs(supply, demand):
    feasible = []
    for s in supply:
        life = remaining_shelf_life_h(s.spoilage_time, CURRENT_TIME, CONFIG.dispatch_buffer_h)
        for d in demand:
            dist = haversine_distance_km(s.lat, s.lon, d.lat, d.lon)
            travel = travel_time_h(dist, CONFIG.avg_speed_kmph)
            if is_feasible(life, travel, d.min_shelf_life_h):
                feasible.append((s, d, dist, life))
    return feasible


def _compute_metrics(matches, supply, demand, runtime_seconds=None):
    matched_kg = sum(m["quantity"] for m in matches)
    supply_kg = sum(s.quantity_kg for s in supply)
    demand_kg = sum(d.quantity_kg for d in demand)
    waste_kg = supply_kg - matched_kg
    unmet_kg = demand_kg - matched_kg
    total_distance = sum(m["distance"] * m["quantity"] for m in matches)
    avg_distance = total_distance / matched_kg if matched_kg > 0 else 0.0
    metrics = {
        "matched_kg": round(matched_kg, 1),
        "waste_kg": round(max(0, waste_kg), 1),
        "unmet_kg": round(max(0, unmet_kg), 1),
        "avg_distance_km": round(avg_distance, 2),
    }
    if runtime_seconds is not None:
        metrics["runtime_seconds"] = round(runtime_seconds, 4)
    return metrics


def _random_assignment(supply, demand):
    start = time.perf_counter()
    feasible = _get_feasible_pairs(supply, demand)
    random.shuffle(feasible)
    matches = []
    used_supply = set()
    used_demand = set()
    for s, d, dist, _ in feasible:
        if s.id in used_supply or d.id in used_demand:
            continue
        qty = min(s.quantity_kg, d.quantity_kg)
        matches.append({"supply": s.id, "demand": d.id, "quantity": qty, "distance": dist})
        used_supply.add(s.id)
        used_demand.add(d.id)
    runtime = time.perf_counter() - start
    return _compute_metrics(matches, supply, demand, runtime_seconds=runtime)


def _greedy_nearest_vendor(supply, demand):
    start = time.perf_counter()
    feasible = _get_feasible_pairs(supply, demand)
    feasible.sort(key=lambda x: x[2])
    matches = []
    used_supply = set()
    used_demand = set()
    for s, d, dist, _ in feasible:
        if s.id in used_supply or d.id in used_demand:
            continue
        qty = min(s.quantity_kg, d.quantity_kg)
        matches.append({"supply": s.id, "demand": d.id, "quantity": qty, "distance": dist})
        used_supply.add(s.id)
        used_demand.add(d.id)
    runtime = time.perf_counter() - start
    return _compute_metrics(matches, supply, demand, runtime_seconds=runtime)


def _aspen_engine(supply, demand):
    start = time.perf_counter()
    result = run_matching_engine(supply, demand, CONFIG, CURRENT_TIME)
    runtime = time.perf_counter() - start

    matches = []
    for crop_result in result.crop_results.values():
        for m in crop_result.matches:
            s = next(lot for lot in supply if lot.id == m.supply_id)
            d = next(order for order in demand if order.id == m.demand_id)
            dist = haversine_distance_km(s.lat, s.lon, d.lat, d.lon)
            matches.append({
                "supply": m.supply_id,
                "demand": m.demand_id,
                "quantity": m.quantity_kg,
                "distance": dist,
            })
    return _compute_metrics(matches, supply, demand, runtime_seconds=runtime)


def run_benchmark() -> dict:
    """
    Run the benchmark and return consistent metric keys for the admin UI.
    """
    supply, demand = _generate_data()
    return {
        "random": _random_assignment(supply, demand),
        "greedy": _greedy_nearest_vendor(supply, demand),
        "aspen": _aspen_engine(supply, demand),
    }