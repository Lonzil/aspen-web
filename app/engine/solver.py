from __future__ import annotations

import logging

import networkx as nx

from .graph_builder import WASTE_NODE, UNMET_NODE, supply_node_name
from .models import SupplyLot, DemandOrder, MatchEdge, CropResult, EngineConfig

logger = logging.getLogger("aspen.engine")


def compute_ui_priority_score(distance_km: float, remaining_life_h: float,
                              config: EngineConfig) -> int:
    bracket = (config.w_distance * (distance_km / config.global_max_distance_km) +
               config.w_freshness * (remaining_life_h / config.global_max_shelf_life_h))
    bracket = max(0.0, min(1.0, bracket))
    return round(100 * (1 - bracket))


def solve_matching(graph, supply_lots, demand_orders, feasible_pairs,
                   edge_costs, infeasible_supply, infeasible_demand,
                   crop_type, config):
    matches = []
    waste_kg = 0.0
    unmet_kg = 0.0

    try:
        flow_dict = nx.min_cost_flow(graph)
    except nx.NetworkXUnfeasible:
        logger.error("NetworkXUnfeasible for crop %s. Check dummy node sizing.", crop_type)
        return CropResult(crop_type, [], waste_kg, unmet_kg,
                          infeasible_supply, infeasible_demand)

    pair_lookup = {(s.id, d.id): (dist_km, life_h)
                   for s, d, dist_km, life_h in feasible_pairs}

    for s in supply_lots:
        s_node = supply_node_name(s.id)
        if s_node not in flow_dict:
            continue
        for target, qty in flow_dict[s_node].items():
            if qty <= 0:
                continue
            if target == WASTE_NODE:
                waste_kg += qty
            elif target.startswith("D:"):
                demand_id = target[2:]
                dist_km, life_h = pair_lookup[(s.id, demand_id)]
                real_cost = edge_costs[(s.id, demand_id)]
                score = compute_ui_priority_score(dist_km, life_h, config)
                matches.append(MatchEdge(
                    supply_id=s.id, demand_id=demand_id,
                    quantity_kg=qty, distance_km=dist_km,
                    cost=real_cost, priority_score=score
                ))

    unmet_flows = flow_dict.get(UNMET_NODE, {})
    for target, qty in unmet_flows.items():
        if qty > 0 and target.startswith("D:"):
            unmet_kg += qty

    return CropResult(crop_type, matches, waste_kg, unmet_kg,
                      infeasible_supply, infeasible_demand)