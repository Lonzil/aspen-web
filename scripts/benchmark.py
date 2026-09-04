"""
ASPEN Performance Benchmark
============================
Compares three matching methods on synthetic supply/demand data:
  1. Random Assignment
  2. Greedy Nearest Vendor
  3. Minimum‑Cost Flow (ASPEN)

Metrics: matched kg, waste kg, unmet kg, average distance.

Run from project root:
    python scripts/benchmark.py
"""

import random
import sys
import time
from datetime import datetime, timedelta

# Ensure app package is importable when running script directly
sys.path.append(".")

from app.engine.models import SupplyLot, DemandOrder, EngineConfig
from app.engine.orchestrator import run_matching_engine
from app.engine.geo import haversine_distance_km, travel_time_h
from app.engine.feasibility import remaining_shelf_life_h, is_feasible

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Synthetic data generation
# ---------------------------------------------------------------------------
def generate_data():
    random.seed(SEED)
    # Faker not needed; use simple random
    supply = []
    demand = []

    # Base coordinates around Accra, Ghana
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


# ---------------------------------------------------------------------------
# Shared feasibility helpers
# ---------------------------------------------------------------------------
def get_feasible_pairs(supply, demand):
    feasible = []
    for s in supply:
        life = remaining_shelf_life_h(s.spoilage_time, CURRENT_TIME, CONFIG.dispatch_buffer_h)
        for d in demand:
            dist = haversine_distance_km(s.lat, s.lon, d.lat, d.lon)
            travel = travel_time_h(dist, CONFIG.avg_speed_kmph)
            if is_feasible(life, travel, d.min_shelf_life_h):
                feasible.append((s, d, dist, life))
    return feasible


def compute_metrics(matches, supply, demand):
    matched_kg = sum(m.get("quantity", 0) for m in matches)
    supply_kg = sum(s.quantity_kg for s in supply)
    demand_kg = sum(d.quantity_kg for d in demand)
    waste_kg = supply_kg - matched_kg
    unmet_kg = demand_kg - matched_kg
    total_distance = sum(m.get("distance", 0) * m.get("quantity", 0) for m in matches)
    avg_distance = total_distance / matched_kg if matched_kg > 0 else 0.0
    return {
        "matched_kg": round(matched_kg, 1),
        "waste_kg": round(max(0, waste_kg), 1),
        "unmet_kg": round(max(0, unmet_kg), 1),
        "avg_distance_km": round(avg_distance, 2),
    }


# ---------------------------------------------------------------------------
# Method 1: Random Assignment
# ---------------------------------------------------------------------------
def random_assignment(supply, demand):
    feasible = get_feasible_pairs(supply, demand)
    random.shuffle(feasible)
    matches = []
    used_supply = {}
    used_demand = {}

    for s, d, dist, life in feasible:
        if s.id in used_supply or d.id in used_demand:
            continue
        qty = min(s.quantity_kg, d.quantity_kg)
        matches.append({"supply": s.id, "demand": d.id, "quantity": qty, "distance": dist})
        used_supply[s.id] = True
        used_demand[d.id] = True

    return compute_metrics(matches, supply, demand)


# ---------------------------------------------------------------------------
# Method 2: Greedy Nearest Vendor
# ---------------------------------------------------------------------------
def greedy_nearest_vendor(supply, demand):
    feasible = get_feasible_pairs(supply, demand)
    # Sort by distance ascending
    feasible.sort(key=lambda x: x[2])
    matches = []
    used_supply = {}
    used_demand = {}

    for s, d, dist, life in feasible:
        if s.id in used_supply or d.id in used_demand:
            continue
        qty = min(s.quantity_kg, d.quantity_kg)
        matches.append({"supply": s.id, "demand": d.id, "quantity": qty, "distance": dist})
        used_supply[s.id] = True
        used_demand[d.id] = True

    return compute_metrics(matches, supply, demand)


# ---------------------------------------------------------------------------
# Method 3: ASPEN (Minimum-Cost Flow)
# ---------------------------------------------------------------------------
def aspen_engine(supply, demand):
    start = time.perf_counter()
    result = run_matching_engine(supply, demand, CONFIG, CURRENT_TIME)
    runtime = time.perf_counter() - start

    matches = []
    for crop_result in result.crop_results.values():
        for m in crop_result.matches:
            # Need distance from supply/demand lookup
            s = next(lot for lot in supply if lot.id == m.supply_id)
            d = next(order for order in demand if order.id == m.demand_id)
            dist = haversine_distance_km(s.lat, s.lon, d.lat, d.lon)
            matches.append({
                "supply": m.supply_id,
                "demand": m.demand_id,
                "quantity": m.quantity_kg,
                "distance": dist,
            })
    metrics = compute_metrics(matches, supply, demand)
    metrics["runtime_seconds"] = round(runtime, 4)
    return metrics


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    supply, demand = generate_data()
    print(f"Generated {len(supply)} supply lots and {len(demand)} demand orders.")

    print("\nRunning Random Assignment...")
    m_random = random_assignment(supply, demand)
    print(m_random)

    print("\nRunning Greedy Nearest Vendor...")
    m_greedy = greedy_nearest_vendor(supply, demand)
    print(m_greedy)

    print("\nRunning ASPEN Engine...")
    m_aspen = aspen_engine(supply, demand)
    print(m_aspen)

    print("\n--- Summary ---")
    print(f"{'Metric':<20}{'Random':<15}{'Greedy':<15}{'ASPEN':<15}")
    for key in ["matched_kg", "waste_kg", "unmet_kg", "avg_distance_km"]:
        print(f"{key:<20}{m_random[key]:<15}{m_greedy[key]:<15}{m_aspen[key]:<15}")