"""
MEC-LifeGuard — Lifecycle Manager (FastAPI)
---------------------------------------------
The brain of the system. Exposes 7 REST endpoints (Section 5.2),
runs a background monitor loop every 5 seconds, and executes
migrations atomically following the 5-step procedure (Section 6.3).

Run: python3 -m uvicorn lcm.main:app --host 0.0.0.0 --port 8000
  or: python3 -m uvicorn lcm.main:app --port 8000
"""

import asyncio
import os
import time
import threading
from contextlib import asynccontextmanager

import requests as http
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from lcm.decision import compute_score, should_migrate, COOLDOWN_S, MIGRATION_SCORE_DELTA
from lcm.topsis import compute_scores
import lcm.migration_log as mig_log

# ─────────────────────────────────────────────
#  Node registry — edge nodes 1 through 10
# ─────────────────────────────────────────────

USE_LOCAL_NODE_URLS = os.getenv("LCM_USE_LOCAL_NODE_URLS", "true").lower() in {"1", "true", "yes"}

if USE_LOCAL_NODE_URLS:
    NODES = {
        f"node{i}": f"http://127.0.0.1:{5000 + i}"
        for i in range(1, 11)
    }
else:
    NODES = {
        f"node{i}": f"http://edge-node-{i}:5000"
        for i in range(1, 11)
    }

MONITOR_INTERVAL_S  = 5      # How often LCM polls nodes and checks migration
HEALTH_POLL_RETRIES = 5      # Retries for health check after deploy
HEALTH_POLL_DELAY_S = 2      # Delay between health check retries
HTTP_TIMEOUT_S      = 5      # Timeout for all outbound HTTP calls (generous for slow nodes)

# ─────────────────────────────────────────────
#  Shared state (protected by a lock)
# ─────────────────────────────────────────────

_lock              = threading.Lock()
_active_node       = None     # type: str | None — node currently serving traffic
_last_migration_ts = 0.0      # type: float     — epoch timestamp of last migration
_migration_running = False    # type: bool      — prevents duplicate concurrent migrations
_node_metrics      = {        # type: dict      — latest polled metrics per node
    nid: {"rtt_ms": 0, "cpu_pct": 0, "score": 0, "status": "unknown"}
    for nid in NODES
}
_start_time        = time.time()  # type: float — LCM process start time


# ─────────────────────────────────────────────
#  HTTP helpers
# ─────────────────────────────────────────────

def _get(url: str):
    """GET with timeout; returns parsed JSON dict or None on any error."""
    try:
        r = http.get(url, timeout=HTTP_TIMEOUT_S)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _post(url: str, **kwargs):
    """POST with timeout; returns parsed JSON dict or None on any error."""
    try:
        r = http.post(url, timeout=HTTP_TIMEOUT_S, **kwargs)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _delete(url: str) -> bool:
    """DELETE with timeout; returns True if response code < 400."""
    try:
        r = http.delete(url, timeout=HTTP_TIMEOUT_S)
        return r.status_code < 400
    except Exception:
        return False


# ─────────────────────────────────────────────
#  Node polling
# ─────────────────────────────────────────────

def _poll_all_nodes() -> dict:
    """Poll /health on all nodes; returns {node_id: metrics_dict}."""
    metrics = {}
    for nid, base_url in NODES.items():
        data = _get(f"{base_url}/health")
        if data:
            failed = data.get("failed", False) or data.get("status") == "failed"
            node_metrics = {
                "rtt_ms":        data.get("rtt_ms", 999),
                "cpu_pct":       data.get("cpu_pct", 0),
                "memory_mb":     data.get("memory_mb", 0),
                "bandwidth_mbps": data.get("bandwidth_mbps", 0),
                "network_type":  data.get("network_type", "unknown"),
                "failed":        failed,
                "status":        data.get("status", "unknown"),
                "deployed":      data.get("deployed", False),
            }
            metrics[nid] = node_metrics
        else:
            metrics[nid] = {
                "rtt_ms":        999,
                "cpu_pct":       0,
                "memory_mb":     0,
                "bandwidth_mbps": 0,
                "network_type":  "unknown",
                "failed":        True,
                "status":        "unreachable",
                "deployed":      False,
                "score":         0,
            }

    ranked_scores = compute_scores(metrics)
    for nid, score in ranked_scores.items():
        metrics[nid]["score"] = score

    return metrics


def _wait_for_ready(node_id: str) -> bool:
    """
    Poll target node's /health until status == 'ready'.
    Returns True if ready within retry limit, False otherwise.
    """
    url = f"{NODES[node_id]}/health"
    for attempt in range(1, HEALTH_POLL_RETRIES + 1):
        data = _get(url)
        if data and data.get("status") == "ready":
            print(f"[LCM] {node_id} ready after {attempt} poll(s)", flush=True)
            return True
        print(
            f"[LCM] Health poll {attempt}/{HEALTH_POLL_RETRIES} "
            f"for {node_id} — not ready yet",
            flush=True,
        )
        time.sleep(HEALTH_POLL_DELAY_S)
    return False


# ─────────────────────────────────────────────
#  Migration execution
# ─────────────────────────────────────────────

def _execute_migration(from_node: str, to_node: str) -> bool:
    """
    5-step atomic migration (Section 6.3).
    Returns True on success, False if any step fails (migration aborted).

    global declarations are at the top of the function — required by Python
    so that 'global x' precedes all references to x in this scope.
    """
    global _active_node, _last_migration_ts, _migration_running

    # ── Acquire migration lock — prevent duplicate concurrent migrations ──
    with _lock:
        if _migration_running:
            print(f"[LCM] Migration already in progress — skipping {from_node}→{to_node}", flush=True)
            return False
        _migration_running = True

    try:
        print(f"[LCM] ════ Migration START: {from_node} → {to_node} ════", flush=True)

        # Step 1: Deploy application on target node
        print(f"[LCM] Step 1: Deploying on {to_node}...", flush=True)
        result = _post(f"{NODES[to_node]}/deploy")
        if not result:
            print(f"[LCM] Step 1 FAILED: POST /deploy on {to_node} returned no response — aborting", flush=True)
            return False
        print(f"[LCM] Step 1 OK: {to_node} deploy response: {result}", flush=True)

        # Step 2: Poll target node until it reports status='ready'
        print(f"[LCM] Step 2: Waiting for {to_node} to be ready...", flush=True)
        if not _wait_for_ready(to_node):
            print(f"[LCM] Step 2 FAILED: {to_node} never became ready — aborting migration", flush=True)
            return False
        print(f"[LCM] Step 2 OK: {to_node} is ready", flush=True)

        # Capture metrics snapshot for the migration log (before switching)
        with _lock:
            before_metrics = dict(_node_metrics.get(from_node, {}))
            after_metrics  = dict(_node_metrics.get(to_node, {}))

        # Step 3: Atomically switch active node pointer and record cooldown timestamp
        print(f"[LCM] Step 3: Switching active node {from_node} → {to_node}", flush=True)
        with _lock:
            _active_node       = to_node
            _last_migration_ts = time.time()
        print(f"[LCM] Step 3 OK: Active node is now {to_node}", flush=True)

        # Step 4: Terminate the old node (fire-and-forget; non-fatal if it fails)
        print(f"[LCM] Step 4: Terminating old node {from_node}...", flush=True)
        ok = _delete(f"{NODES[from_node]}/terminate")
        print(f"[LCM] Step 4 {'OK' if ok else 'WARN (terminate call failed, continuing)'}: {from_node} terminated", flush=True)

        # Step 5: Append migration event to persistent log
        event = mig_log.append(
            from_node           = from_node,
            to_node             = to_node,
            latency_before_ms   = before_metrics.get("rtt_ms", 0),
            latency_after_ms    = after_metrics.get("rtt_ms", 0),
            score_before        = before_metrics.get("score", 0),
            score_after         = after_metrics.get("score", 0),
            network_before      = before_metrics.get("network_type", "unknown"),
            network_after       = after_metrics.get("network_type", "unknown"),
            bandwidth_before    = before_metrics.get("bandwidth_mbps", 0),
            bandwidth_after     = after_metrics.get("bandwidth_mbps", 0),
            topsis_before       = before_metrics.get("score", 0),
            topsis_after        = after_metrics.get("score", 0),
        )
        print(f"[LCM] Step 5 OK: Migration #{mig_log.count()} logged — "
              f"latency {before_metrics.get('rtt_ms',0):.1f}ms → {after_metrics.get('rtt_ms',0):.1f}ms",
              flush=True)
        print(f"[LCM] ════ Migration COMPLETE: {from_node} → {to_node} ════", flush=True)
        return True

    except Exception as e:
        print(f"[LCM] Migration EXCEPTION: {e}", flush=True)
        return False

    finally:
        # Always release the migration lock, even on failure
        with _lock:
            _migration_running = False


# ─────────────────────────────────────────────
#  Background monitor loop
# ─────────────────────────────────────────────

async def _monitor_loop():
    """
    Runs every MONITOR_INTERVAL_S seconds.
    Polls all nodes, updates the metrics cache, evaluates migration trigger.
    """
    cycle = 0
    while True:
        await asyncio.sleep(MONITOR_INTERVAL_S)
        cycle += 1

        # Poll all nodes (network I/O — done outside the lock)
        metrics = _poll_all_nodes()

        # Update shared metrics cache; snapshot mutable state for decision logic
        with _lock:
            _node_metrics.update(metrics)
            active          = _active_node
            last_mig        = _last_migration_ts
            already_running = _migration_running

        healthy_count = sum(1 for m in metrics.values() if not m.get("failed", False))
        best_node = max(metrics.items(), key=lambda item: item[1].get("score", 0))[0]
        best_score = metrics[best_node].get("score", 0)

        print(
            f"[LCM] Monitor cycle {cycle} | active={active} | healthy={healthy_count}/10 | "
            f"best={best_node}(score={best_score:.3f})",
            flush=True,
        )

        # Extra debug info: show migration policy parameters and cooldown status
        time_since_last_mig = time.time() - last_mig
        print(
            f"[LCM] Policy: threshold={MIGRATION_SCORE_DELTA} | cooldown_s={COOLDOWN_S} | "
            f"since_last_mig={time_since_last_mig:.0f}s",
            flush=True,
        )

        if active is None:
            print(f"[LCM] Monitor cycle {cycle}: no app deployed yet — skipping", flush=True)
            continue

        if already_running:
            print(f"[LCM] Monitor cycle {cycle}: migration already in progress — skipping evaluation", flush=True)
            continue

        # Decision evaluation (logs produced in should_migrate)
        fire, target = should_migrate(active, metrics, last_mig)

        if fire and target:
            print(
                f"[LCM] ★ MIGRATION TRIGGERED: {active}(score={metrics[active].get('score',0):.3f}) "
                f"→ {target}(score={metrics[target].get('score',0):.3f})",
                flush=True,
            )
            # Run in a daemon thread so the async event loop stays unblocked
            threading.Thread(
                target=_execute_migration,
                args=(active, target),
                daemon=True,
            ).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the background monitor loop when the app starts; cancel on shutdown."""
    task = asyncio.create_task(_monitor_loop())
    print("[LCM] Monitor loop started", flush=True)
    yield
    task.cancel()


# ─────────────────────────────────────────────
#  FastAPI application
# ─────────────────────────────────────────────

app = FastAPI(
    title="MEC-LifeGuard LCM",
    description="Application Lifecycle Manager — ETSI MEC-inspired",
    version="1.0.0",
    lifespan=lifespan,
)


# ─── Endpoint 1: Deploy ──────────────────────

@app.post("/app/deploy", summary="Deploy application on best available node")
def deploy_app():
    """
    Poll all nodes, pick the one with the lowest score, and deploy there.
    Returns the chosen node and its current metrics.

    FIX: global _active_node declared at the top of the function, before
    the _node_metrics.update() call that references module-level state.
    """
    global _active_node   # ← declared FIRST

    metrics = _poll_all_nodes()
    with _lock:
        _node_metrics.update(metrics)

    best = max(
        (nid for nid, m in metrics.items() if not m.get("failed", False)),
        key=lambda nid: metrics[nid].get("score", 0),
        default=None,
    )

    if best is None:
        raise HTTPException(status_code=502, detail="No healthy nodes available for deployment")

    result = _post(f"{NODES[best]}/deploy")
    if not result:
        raise HTTPException(status_code=502, detail=f"Failed to deploy on {best}")

    with _lock:
        _active_node = best

    return {
        "status":        "deployed",
        "active_node":   best,
        "topsis_score":  round(metrics[best]["score"], 3),
        "rtt_ms":        metrics[best]["rtt_ms"],
        "cpu_pct":       metrics[best]["cpu_pct"],
        "memory_mb":     metrics[best]["memory_mb"],
        "bandwidth_mbps": metrics[best]["bandwidth_mbps"],
        "network_type":  metrics[best]["network_type"],
    }


# ─── Endpoint 2: Status ──────────────────────

@app.get("/app/status", summary="Query active node and current metrics")
def app_status():
    """Return the active node ID plus its latest RTT, CPU, score, and uptime."""
    with _lock:
        active  = _active_node
        metrics = dict(_node_metrics)

    if active is None:
        return {"status": "not_deployed", "active_node": None}

    m = metrics.get(active, {})
    return {
        "status":          "running",
        "active_node":     active,
        "rtt_ms":          m.get("rtt_ms", 0),
        "cpu_pct":         m.get("cpu_pct", 0),
        "memory_mb":       m.get("memory_mb", 0),
        "bandwidth_mbps":  m.get("bandwidth_mbps", 0),
        "network_type":    m.get("network_type", "unknown"),
        "topsis_score":    round(m.get("score", 0), 3),
        "uptime_s":        round(time.time() - _start_time, 1),
        "migration_count": mig_log.count(),
    }


# ─── Endpoint 3: Manual migrate ──────────────

class MigrateRequest(BaseModel):
    target_node: str


@app.post("/app/migrate", summary="Manually trigger migration to a specified node")
def migrate_app(body: MigrateRequest):
    """Manually migrate the application to any named node."""
    with _lock:
        active = _active_node

    if active is None:
        raise HTTPException(status_code=400, detail="No application deployed")
    if body.target_node not in NODES:
        raise HTTPException(status_code=400, detail=f"Unknown node: {body.target_node}")
    if body.target_node == active:
        raise HTTPException(status_code=400, detail="Target is already the active node")

    success = _execute_migration(active, body.target_node)
    if not success:
        raise HTTPException(
            status_code=502, detail="Migration failed — health check timeout"
        )

    return {"status": "migrated", "from": active, "to": body.target_node}


# ─── Endpoint 4: Terminate ───────────────────

@app.delete("/app/terminate", summary="Stop application on active node")
def terminate_app():
    """
    Terminate the currently deployed application and clear the active node pointer.

    FIX: global _active_node declared at the top of the function, before the
    first with _lock block that reads _active_node.
    """
    global _active_node   # ← declared FIRST

    with _lock:
        active = _active_node

    if active is None:
        raise HTTPException(status_code=400, detail="No application deployed")

    ok = _delete(f"{NODES[active]}/terminate")

    with _lock:
        _active_node = None

    return {"status": "terminated", "node": active, "success": ok}


# ─── Endpoint 5: Node discovery ──────────────

@app.get("/nodes", summary="List all edge nodes with current scores")
def list_nodes():
    """Return all registered nodes with their latest RTT, CPU, score, and active flag."""
    with _lock:
        metrics = dict(_node_metrics)
        active  = _active_node

    return {
        "nodes": [
            {
                "id":     nid,
                "url":    NODES[nid],
                "active": nid == active,
                **metrics.get(nid, {}),
            }
            for nid in NODES
        ]
    }


# ─── Debug endpoints ─────────────────────────

@app.get("/debug/topsis", summary="Debug: TOPSIS decision snapshot")
def debug_topsis():
    """Return current TOPSIS scores, active/best nodes, threshold and cooldown."""
    with _lock:
        metrics = dict(_node_metrics)
        active = _active_node
        last_mig = _last_migration_ts

    scores = compute_scores(metrics)
    best = max(scores.items(), key=lambda it: it[1])[0] if scores else None
    cooldown_elapsed = time.time() - last_mig

    return {
        "active_node": active,
        "best_node": best,
        "scores": {nid: round(s, 4) for nid, s in scores.items()},
        "threshold": MIGRATION_SCORE_DELTA,
        "cooldown_s": COOLDOWN_S,
        "cooldown_elapsed_s": round(cooldown_elapsed, 1),
    }


@app.get("/debug/network", summary="Debug: current network for active node")
def debug_network():
    """Return the network_type and bandwidth for the active node (or aggregated info)."""
    with _lock:
        active = _active_node
        metrics = dict(_node_metrics)

    if active and active in metrics:
        m = metrics[active]
        return {"network_type": m.get("network_type", "unknown"), "bandwidth_mbps": m.get("bandwidth_mbps", 0)}

    # Fallback: return most common network among nodes
    net_counts = {}
    for m in metrics.values():
        nt = m.get("network_type", "unknown")
        net_counts[nt] = net_counts.get(nt, 0) + 1
    if not net_counts:
        return {"network_type": "unknown", "bandwidth_mbps": 0}
    most_common = max(net_counts.items(), key=lambda it: it[1])[0]
    avg_bw = sum(m.get("bandwidth_mbps", 0) for m in metrics.values()) / max(1, len(metrics))
    return {"network_type": most_common, "bandwidth_mbps": round(avg_bw, 2)}


# ─── Endpoint 6: Migration history ───────────

@app.get("/migrations", summary="Full migration event log")
def migration_history():
    """Return all recorded migration events with before/after latency."""
    return {"count": mig_log.count(), "events": mig_log.get_all()}


# ─── Endpoint 7: Prometheus metrics ──────────

@app.get("/metrics", summary="Prometheus-format metrics (plain text)")
def prometheus_metrics():
    """Emit all node metrics in Prometheus exposition format."""
    with _lock:
        metrics = dict(_node_metrics)
        active  = _active_node

    lines = []
    for nid, m in metrics.items():
        lines += [
            f'edge_rtt_ms{{node="{nid}"}} {m.get("rtt_ms", 0)}',
            f'edge_cpu_pct{{node="{nid}"}} {m.get("cpu_pct", 0)}',
            f'edge_memory_mb{{node="{nid}"}} {m.get("memory_mb", 0)}',
            f'edge_bandwidth_mbps{{node="{nid}"}} {m.get("bandwidth_mbps", 0)}',
            f'edge_network_score{{node="{nid}"}} {m.get("score", 0)}',
            f'edge_failed{{node="{nid}"}} {1 if m.get("failed") else 0}',
            f'edge_active{{node="{nid}"}} {1 if nid == active else 0}',
        ]
    lines.append(f"lcm_migration_total {mig_log.count()}")

    return PlainTextResponse("\n".join(lines))


# ─────────────────────────────────────────────
#  Entry point (for direct python run)
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("lcm.main:app", host="0.0.0.0", port=8000, reload=False)
