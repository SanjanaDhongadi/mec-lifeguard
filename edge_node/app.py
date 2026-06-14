"""
MEC-LifeGuard — Edge Node (Flask)
----------------------------------
Each edge node is a Docker container running this Flask app.
NODE_ID is set via environment variable (node1 through node10).

Key trick: before responding, the app reads its current RTT from
mobility_state.json and sleeps that long — making latency REAL
from the client's perspective, even though it's simulated.
"""

import os
import json
import time
import random
import threading
from pathlib import Path

import psutil
from flask import Flask, jsonify, request

app = Flask(__name__)

NODE_ID = os.environ.get("NODE_ID", "node1")

# Path to the shared mobility state (mounted as a volume)
MOBILITY_STATE_PATH = Path("/shared/mobility_state.json")

# Application lifecycle state
app_state = {
    "deployed": False,
    "deploy_time": None,
    "request_count": 0,
}
state_lock = threading.Lock()


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

def _load_shared_state() -> dict:
    """Read the shared mobility state file safely."""
    try:
        with open(MOBILITY_STATE_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return {}


def get_node_state() -> dict:
    """Return the current simulated state for this node, including network info."""
    t0 = time.time()
    shared = _load_shared_state()
    t_shared = time.time()
    node_state = shared.get(NODE_ID, {})

    failed = bool(node_state.get("failed", False))
    base_rtt = float(node_state.get("rtt_ms", 50))
    # Use a non-blocking psutil call if the simulator did not provide cpu_pct
    t_psutil = None
    if "cpu_pct" in node_state:
        cpu_pct = float(node_state.get("cpu_pct", 0.0))
    else:
        t_ps_start = time.time()
        cpu_pct = float(psutil.cpu_percent(interval=0.0))
        t_psutil = (time.time() - t_ps_start) * 1000.0

    memory_mb = float(node_state.get("memory_mb", 512.0))

    if failed:
        base_rtt = max(base_rtt, 999.0)
        cpu_pct = 100.0

    noisy_rtt = base_rtt + random.gauss(0, 5)
    rtt_ms = max(1.0, noisy_rtt)
    t_after = time.time()
    shared_read_ms = (t_shared - t0) * 1000.0

    out = {
        "rtt_ms":       round(rtt_ms, 2),
        "cpu_pct":      round(min(max(cpu_pct, 0.0), 100.0), 2),
        "memory_mb":    round(max(memory_mb, 0.0), 2),
        "failed":       failed,
        "network_type": shared.get("network_type", "unknown"),
        "bandwidth_mbps": round(float(shared.get("bandwidth_mbps", 0)), 2),
        "shared_read_ms": round(shared_read_ms, 2),
    }
    if t_psutil is not None:
        out["psutil_ms"] = round(t_psutil, 2)
    return out


def get_my_rtt() -> float:
    """Return a realistic RTT for this node from the shared state."""
    return get_node_state()["rtt_ms"]


def get_cpu_pct() -> float:
    """Return the simulated CPU percentage for this node."""
    return get_node_state()["cpu_pct"]


# ─────────────────────────────────────────────
#  Endpoints
# ─────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    """
    LCM polls this to check if the node is alive and whether
    a deployed app is ready to serve traffic.
    """
    state = get_node_state()

    with state_lock:
        deployed = app_state["deployed"]

    status = "failed" if state["failed"] else ("ready" if deployed else "idle")

    return jsonify({
        "node": NODE_ID,
        "status": status,
        "rtt_ms": state["rtt_ms"],
        "cpu_pct": state["cpu_pct"],
        "memory_mb": state["memory_mb"],
        "bandwidth_mbps": state["bandwidth_mbps"],
        "network_type": state["network_type"],
        "failed": state["failed"],
        "deployed": deployed,
    }), 200


@app.route("/process", methods=["POST"])
def process():
    """
    The main request endpoint — simulates processing a user's workload.
    Injects RTT-based latency via time.sleep() before responding.
    Client measures end-to-end response time = simulated latency.
    """
    t_entry = time.time()
    state = get_node_state()
    if state["failed"]:
        return jsonify({"error": "Node is failed and cannot process requests"}), 503

    with state_lock:
        if not app_state["deployed"]:
            return jsonify({"error": "No application deployed on this node"}), 503
        app_state["request_count"] += 1
        count = app_state["request_count"]

    rtt_ms = state["rtt_ms"]
    cpu = state["cpu_pct"]

    # THIS is what makes latency real from the client's perspective
    time.sleep(rtt_ms / 1000.0)

    t_exit = time.time()
    server_total_ms = (t_exit - t_entry) * 1000.0
    server_pretend_ms = max(0.0, server_total_ms - rtt_ms)

    payload = {
        "result": "processed",
        "node": NODE_ID,
        "simulated_rtt_ms": rtt_ms,
        "cpu_pct": cpu,
        "memory_mb": state["memory_mb"],
        "bandwidth_mbps": state["bandwidth_mbps"],
        "network_type": state["network_type"],
        "request_count": count,
        "server_pretend_ms": round(server_pretend_ms, 2),
        "server_total_ms": round(server_total_ms, 2),
    }

    # include any debug timing from get_node_state
    if "shared_read_ms" in state:
        payload["shared_read_ms"] = state["shared_read_ms"]
    if "psutil_ms" in state:
        payload["psutil_ms"] = state["psutil_ms"]

    return jsonify(payload), 200


@app.route("/deploy", methods=["POST"])
def deploy():
    """
    LCM calls this to deploy the application on this node.
    In a real system this would pull a container image.
    Here it just flips a flag and records the deploy time.
    """
    with state_lock:
        if app_state["deployed"]:
            return jsonify({"status": "already_deployed", "node": NODE_ID}), 200
        app_state["deployed"] = True
        app_state["deploy_time"] = time.time()
        app_state["request_count"] = 0

    print(f"[{NODE_ID}] Application deployed", flush=True)
    return jsonify({"status": "deployed", "node": NODE_ID}), 200


@app.route("/terminate", methods=["DELETE"])
def terminate():
    """
    LCM calls this after migrating traffic away from this node.
    Unloads the application (resets state).
    """
    with state_lock:
        if not app_state["deployed"]:
            return jsonify({"status": "not_deployed", "node": NODE_ID}), 200
        app_state["deployed"] = False
        app_state["deploy_time"] = None
        app_state["request_count"] = 0

    print(f"[{NODE_ID}] Application terminated", flush=True)
    return jsonify({"status": "terminated", "node": NODE_ID}), 200


@app.route("/metrics", methods=["GET"])
def metrics():
    """
    Prometheus-format metrics endpoint (plain text).
    Optional: plug into Grafana for a visual dashboard.
    """
    state = get_node_state()

    with state_lock:
        deployed = int(app_state["deployed"])
        req_count = app_state["request_count"]

    lines = [
        f'# HELP edge_node_rtt_ms Simulated RTT in milliseconds',
        f'# TYPE edge_node_rtt_ms gauge',
        f'edge_node_rtt_ms{{node="{NODE_ID}"}} {state["rtt_ms"]}',
        f'',
        f'# HELP edge_node_cpu_pct CPU usage percentage',
        f'# TYPE edge_node_cpu_pct gauge',
        f'edge_node_cpu_pct{{node="{NODE_ID}"}} {state["cpu_pct"]}',
        f'',
        f'# HELP edge_node_memory_mb Memory usage in megabytes',
        f'# TYPE edge_node_memory_mb gauge',
        f'edge_node_memory_mb{{node="{NODE_ID}"}} {state["memory_mb"]}',
        f'',
        f'# HELP edge_node_bandwidth_mbps Current uplink bandwidth',
        f'# TYPE edge_node_bandwidth_mbps gauge',
        f'edge_node_bandwidth_mbps{{node="{NODE_ID}"}} {state["bandwidth_mbps"]}',
        f'',
        f'# HELP edge_node_failed Whether the node is in failed state',
        f'# TYPE edge_node_failed gauge',
        f'edge_node_failed{{node="{NODE_ID}"}} {1 if state["failed"] else 0}',
        f'',
        f'# HELP edge_node_deployed Whether application is deployed (1/0)',
        f'# TYPE edge_node_deployed gauge',
        f'edge_node_deployed{{node="{NODE_ID}"}} {deployed}',
        f'',
        f'# HELP edge_node_request_total Total requests processed',
        f'# TYPE edge_node_request_total counter',
        f'edge_node_request_total{{node="{NODE_ID}"}} {req_count}',
    ]
    return "\n".join(lines), 200, {"Content-Type": "text/plain; charset=utf-8"}


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[{NODE_ID}] Edge node starting on port 5000", flush=True)
    app.run(host="0.0.0.0", port=5000, debug=False)
