from __future__ import annotations

from typing import Dict, Tuple

import networkx as nx

from .models import SupplyLot, DemandOrder, EngineConfig
from .feasibility import edge_cost

COST_SCALE = 1000
WASTE_NODE = "__WASTE__"
UNMET_NODE = "__UNMET__"
MF_SRC = "__MF_SRC__"
MF_SNK = "__MF_SNK__"


def supply_node_name(supply_id: str) -> str:
    return f"S:{supply_id}"


def demand_node_name(demand_id: str) -> str:
    return f"D:{demand_id}"


def _max_real_flow(supply_lots, demand_orders, feasible_pairs) -> int:
    """Phase A: maximum achievable real-matching flow (feasible edges only)."""
    G = nx.DiGraph()
    for s in supply_lots:
        G.add_edge(MF_SRC, supply_node_name(s.id), capacity=int(round(s.quantity_kg)))
    for d in demand_orders:
        G.add_edge(demand_node_name(d.id), MF_SNK, capacity=int(round(d.quantity_kg)))
    for s, d, _, _ in feasible_pairs:
        cap = int(round(min(s.quantity_kg, d.quantity_kg)))
        G.add_edge(supply_node_name(s.id), demand_node_name(d.id), capacity=cap)
    if MF_SRC not in G or MF_SNK not in G:
        return 0
    return int(nx.maximum_flow_value(G, MF_SRC, MF_SNK))


def compute_dynamic_penalty(total_real_flow: float) -> int:
    return int(COST_SCALE * total_real_flow) + 1


def build_flow_graph(supply_lots, demand_orders, feasible_pairs,
                     edge_costs: Dict[Tuple[str, str], float],
                     config: EngineConfig) -> nx.DiGraph:
    graph = nx.DiGraph()

    # Big enough capacity for edges (never binding)
    total_supply = sum(s.quantity_kg for s in supply_lots)
    total_demand = sum(d.quantity_kg for d in demand_orders)
    big_capacity = int(total_supply + total_demand) + 1

    # Phase A – true achievable matching flow (integer)
    max_flow = _max_real_flow(supply_lots, demand_orders, feasible_pairs)

    # --- Real nodes, accumulate integer demands ---
    supply_int_sum = 0
    demand_int_sum = 0

    for s in supply_lots:
        val = int(round(s.quantity_kg))
        graph.add_node(supply_node_name(s.id), demand=-val)
        supply_int_sum += val

    for d in demand_orders:
        val = int(round(d.quantity_kg))
        graph.add_node(demand_node_name(d.id), demand=val)
        demand_int_sum += val

    # --- Dummy nodes using the already‑rounded integer totals ---
    waste_demand = supply_int_sum - max_flow
    unmet_demand = -(demand_int_sum - max_flow)

    graph.add_node(WASTE_NODE, demand=waste_demand)
    graph.add_node(UNMET_NODE, demand=unmet_demand)

    # --- Real edges ---
    for s, d, _, _ in feasible_pairs:
        real_cost = edge_costs[(s.id, d.id)]
        scaled_cost = int(round(real_cost * COST_SCALE))
        graph.add_edge(supply_node_name(s.id), demand_node_name(d.id),
                       capacity=big_capacity, weight=scaled_cost)

    # --- Penalty (still using float min for magnitude, but integer max_flow is fine) ---
    penalty = compute_dynamic_penalty(float(min(total_supply, total_demand)))

    # Waste edges (every supply → T)
    for s in supply_lots:
        graph.add_edge(supply_node_name(s.id), WASTE_NODE,
                       capacity=big_capacity, weight=penalty)

    # Unmet edges (S* → every demand)
    for d in demand_orders:
        graph.add_edge(UNMET_NODE, demand_node_name(d.id),
                       capacity=big_capacity, weight=penalty)

    # Balance verification
    total_demand_sum = sum(nx.get_node_attributes(graph, "demand").values())
    if total_demand_sum != 0:
        raise ValueError(f"Graph is not balanced: sum(demands) = {total_demand_sum}")
    return graph