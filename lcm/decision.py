"""
MEC-LifeGuard — LCM Decision Engine
--------------------------------------
TOPSIS-based migration ranking with cooldown-based migration triggers.

CHANGES (task 2 fix):
  - When the active node's network_type is Bluetooth, migration is forced
    immediately (bypasses cooldown) if any healthy non-Bluetooth node exists.
  - This ensures Bluetooth never stays active as the serving node for long.
"""

import time
from typing import Any

from lcm.topsis import compute_scores

COOLDOWN_S            = 30     # seconds to wait between normal migrations
MIGRATION_SCORE_DELTA = 0.02   # minimum TOPSIS advantage to trigger migration

# Networks considered poor quality — trigger forced migration
POOR_NETWORKS = {"Bluetooth"}


def compute_score(metrics: dict[str, Any]) -> float:
    if not metrics or metrics.get("failed"):
        return 0.0
    scores = compute_scores({"node": metrics})
    return scores.get("node", 0.0)


def should_migrate(
    active_node_id: str,
    node_metrics: dict[str, dict],
    last_migration_time: float,
) -> tuple[bool, str | None]:
    """Decide whether to migrate from the active node to a better healthy node.

    Returns (should_migrate: bool, target_node_id: str | None).
    """
    active_metrics = node_metrics.get(active_node_id)
    if active_metrics is None:
        print(f"[Decision] active node '{active_node_id}' missing from metrics — skip", flush=True)
        return False, None

    healthy_nodes = {
        nid: metrics
        for nid, metrics in node_metrics.items()
        if not metrics.get("failed", False)
    }

    if not healthy_nodes:
        print("[Decision] no healthy nodes available — skip", flush=True)
        return False, None

    scores = compute_scores(healthy_nodes)
    if not scores:
        print("[Decision] unable to compute TOPSIS scores — skip", flush=True)
        return False, None

    # ── Failover: active node is unhealthy ───────────────────────────────
    if active_node_id not in scores:
        best_node_id = max(scores, key=scores.__getitem__)
        print(
            f"[Decision] active node '{active_node_id}' is unhealthy — failover to {best_node_id}",
            flush=True,
        )
        return True, best_node_id

    active_score        = scores[active_node_id]
    active_network_type = active_metrics.get("network_type", "unknown")
    candidates          = {nid: score for nid, score in scores.items() if nid != active_node_id}

    if not candidates:
        return False, None

    best_node_id = max(candidates, key=candidates.__getitem__)
    best_score   = candidates[best_node_id]
    delta        = best_score - active_score
    time_since   = time.time() - last_migration_time

    # ── Bluetooth forced migration (bypasses cooldown) ───────────────────
    if active_network_type in POOR_NETWORKS:
        # find best candidate on a non-poor network
        non_poor_candidates = {
            nid: score for nid, score in candidates.items()
            if node_metrics[nid].get("network_type", "unknown") not in POOR_NETWORKS
        }
        if non_poor_candidates:
            target = max(non_poor_candidates, key=non_poor_candidates.__getitem__)
            print(
                f"[Decision] ⚠ FORCED MIGRATION: active node on {active_network_type} "
                f"— migrating to {target} "
                f"(score {active_score:.3f} → {non_poor_candidates[target]:.3f}, cooldown bypassed)",
                flush=True,
            )
            return True, target
        else:
            print(
                f"[Decision] Active on {active_network_type} but no non-{active_network_type} "
                "candidates available — staying put",
                flush=True,
            )

    # ── Normal TOPSIS migration ──────────────────────────────────────────
    condition_score    = best_score > active_score + MIGRATION_SCORE_DELTA
    condition_cooldown = time_since >= COOLDOWN_S

    print(
        f"[Decision] active={active_node_id}(score={active_score:.3f}, net={active_network_type}) "
        f"best={best_node_id}(score={best_score:.3f}) | "
        f"Δ={delta:.3f} threshold={MIGRATION_SCORE_DELTA} | "
        f"cooldown {time_since:.0f}s/{COOLDOWN_S}s → {condition_cooldown}",
        flush=True,
    )

    if condition_score and condition_cooldown:
        return True, best_node_id

    return False, None