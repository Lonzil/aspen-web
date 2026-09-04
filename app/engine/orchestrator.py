from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime
from typing import List, Optional

from .models import SupplyLot, DemandOrder, EngineConfig, EngineResult
from .feasibility import build_feasible_pairs, compute_d_max_l_max, edge_cost
from .graph_builder import build_flow_graph
from .solver import solve_matching

logger = logging.getLogger("aspen.engine")


def run_matching_engine(supply_lots: List[SupplyLot],
                        demand_orders: List[DemandOrder],
                        config: Optional[EngineConfig] = None,
                        current_time: Optional[datetime] = None) -> EngineResult:
    if config is None:
        config = EngineConfig()
    if current_time is None:
        current_time = datetime.now()

    start = time.perf_counter()
    result = EngineResult()

    supply_by_crop = defaultdict(list)
    demand_by_crop = defaultdict(list)
    for s in supply_lots:
        supply_by_crop[s.crop_type].append(s)
    for d in demand_orders:
        demand_by_crop[d.crop_type].append(d)

    all_crops = set(supply_by_crop) | set(demand_by_crop)

    for crop in sorted(all_crops):
        try:
            S = supply_by_crop.get(crop, [])
            D = demand_by_crop.get(crop, [])

            feasible, inf_s, inf_d = build_feasible_pairs(S, D, config, current_time)
            d_max, l_max = compute_d_max_l_max(feasible)

            edge_costs = {}
            for s, d, dist_km, life_h in feasible:
                edge_costs[(s.id, d.id)] = edge_cost(dist_km, life_h, d_max, l_max, config)

            graph = build_flow_graph(S, D, feasible, edge_costs, config)

            crop_result = solve_matching(graph, S, D, feasible, edge_costs,
                                         inf_s, inf_d, crop, config)
            result.crop_results[crop] = crop_result
        except Exception as e:
            logger.exception("Engine failed for crop %s", crop)
            # continue with other crops

    result.runtime_seconds = time.perf_counter() - start
    return result