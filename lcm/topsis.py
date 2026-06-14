"""
MEC-LifeGuard — TOPSIS Decision Engine
--------------------------------------
Builds a TOPSIS decision matrix for edge node selection.

Criteria:
  - RTT (lower better)
  - CPU Usage (lower better)
  - Memory Usage (lower better)
  - Bandwidth (higher better)
  - Migration Cost (lower better)
  - Network Score (higher better)

Network Score mapping:
  Bluetooth = 1, 4G = 2, 5G = 3, 6G = 4

Weights (UPDATED — Bluetooth now penalised via bandwidth weight bump):
  RTT            = 0.30
  CPU            = 0.20
  Memory         = 0.10
  Bandwidth      = 0.20   ← increased from 0.15 so low BT bandwidth hurts score
  Migration Cost = 0.05
  Network Score  = 0.15   ← increased from 0.10 so Bluetooth score=1 pulls rank down

CHANGES (task 2 fix):
  - Bluetooth network_score is kept at 1.0 but its weight is now 0.15
  - bandwidth weight raised to 0.20 (Bluetooth 1-5 Mbps vs 5G 100-1000 Mbps
    now creates a clear score gap that reliably triggers migration)
  - bluetooth_penalty field added to _build_node_metrics:
    when network_type == Bluetooth, effective RTT is inflated by 40ms
    so the cost criterion also penalises it
"""

import math
from typing import Dict, Any

NETWORK_SCORE = {
    "Bluetooth": 1.0,
    "4G":        2.0,
    "5G":        3.0,
    "6G":        4.0,
}

CRITERIA = [
    "rtt_ms",
    "cpu_pct",
    "memory_mb",
    "bandwidth_mbps",
    "migration_cost",
    "network_score",
]

BENEFIT_CRITERIA = {"bandwidth_mbps", "network_score"}
COST_CRITERIA    = {"rtt_ms", "cpu_pct", "memory_mb", "migration_cost"}

WEIGHTS = {
    "rtt_ms":        0.30,
    "cpu_pct":       0.20,
    "memory_mb":     0.10,
    "bandwidth_mbps": 0.20,   # ← raised (punishes Bluetooth's 1-5 Mbps)
    "migration_cost": 0.05,
    "network_score":  0.15,   # ← raised (Bluetooth score=1 now meaningfully hurts rank)
}

# Sanity check — weights must sum to 1.0
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "TOPSIS weights must sum to 1.0"

BLUETOOTH_RTT_PENALTY_MS = 40.0   # Added to effective RTT when on Bluetooth


def _normalize_column(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values))
    if norm <= 0:
        return [0.0 for _ in values]
    return [v / norm for v in values]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _build_node_metrics(node_data: dict) -> dict[str, float]:
    """Extract and normalise raw node metrics for TOPSIS.

    Bluetooth penalty: effective RTT is inflated so that even nodes that
    happen to have low geometric RTT are penalised when on Bluetooth.
    """
    bandwidth   = max(_safe_float(node_data.get("bandwidth_mbps"), 0.0), 0.0)
    memory      = max(_safe_float(node_data.get("memory_mb"),      0.0), 0.0)
    network_type = node_data.get("network_type", "unknown")
    raw_rtt      = _safe_float(node_data.get("rtt_ms"), 999.0)

    # Apply Bluetooth RTT penalty so cost criterion also fires
    if network_type == "Bluetooth":
        effective_rtt = raw_rtt + BLUETOOTH_RTT_PENALTY_MS
    else:
        effective_rtt = raw_rtt

    return {
        "rtt_ms":        effective_rtt,
        "cpu_pct":       _safe_float(node_data.get("cpu_pct"),  100.0),
        "memory_mb":     memory,
        "bandwidth_mbps": bandwidth,
        "network_score": NETWORK_SCORE.get(network_type, 0.0),
        "migration_cost": memory / max(bandwidth, 1.0),
    }


def compute_scores(nodes: dict[str, dict]) -> dict[str, float]:
    """Compute TOPSIS closeness coefficients for multiple nodes."""
    if not nodes:
        return {}

    node_ids   = list(nodes.keys())
    raw_matrix = {nid: _build_node_metrics(nodes[nid]) for nid in node_ids}

    matrix_by_criterion: dict[str, list[float]] = {
        criterion: [raw_matrix[nid][criterion] for nid in node_ids]
        for criterion in CRITERIA
    }

    normalized: dict[str, list[float]] = {
        criterion: _normalize_column(values)
        for criterion, values in matrix_by_criterion.items()
    }

    weighted: dict[str, list[float]] = {
        criterion: [value * WEIGHTS[criterion] for value in normalized[criterion]]
        for criterion in CRITERIA
    }

    ideal_best:  dict[str, float] = {}
    ideal_worst: dict[str, float] = {}
    for criterion in CRITERIA:
        values = weighted[criterion]
        if criterion in BENEFIT_CRITERIA:
            ideal_best[criterion]  = max(values)
            ideal_worst[criterion] = min(values)
        else:
            ideal_best[criterion]  = min(values)
            ideal_worst[criterion] = max(values)

    scores: dict[str, float] = {}
    for index, nid in enumerate(node_ids):
        positive_delta = 0.0
        negative_delta = 0.0
        for criterion in CRITERIA:
            value = weighted[criterion][index]
            positive_delta += (value - ideal_best[criterion])  ** 2
            negative_delta += (value - ideal_worst[criterion]) ** 2

        sep_pos     = math.sqrt(positive_delta)
        sep_neg     = math.sqrt(negative_delta)
        denominator = sep_pos + sep_neg
        scores[nid] = 0.0 if denominator <= 0 else sep_neg / denominator

    return scores


def rank_nodes(nodes: dict[str, dict]) -> list[tuple[str, float]]:
    """Return nodes sorted by descending TOPSIS score."""
    scores = compute_scores(nodes)
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)