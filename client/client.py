"""
MEC-LifeGuard — Simulated Client
-----------------------------------
Simulates the mobile user's application continuously sending requests.
Every second it:
  1. Asks the LCM which node is active (GET /app/status)
  2. Sends a POST /process to that node
  3. Measures the actual round-trip time
  4. Appends to latency_log.csv

When the LCM migrates to a new node, the client automatically follows —
it re-queries /app/status every iteration and uses whatever node LCM reports.

Run: python client/client.py
"""

import csv
import sys
import time
import signal
import random
from pathlib import Path

import requests

LCM_URL         = "http://localhost:8000"
HTTP_TIMEOUT_S   = 10           # generous timeout (node may sleep up to ~100ms)
REQUEST_INTERVAL = 1.0
LOG_PATH         = Path(__file__).parent / "latency_log.csv"
CSV_HEADERS      = [
    "elapsed_s", "timestamp", "node", "rtt_ms", "status",
    "network_type", "bandwidth_mbps", "topsis_score",
    "server_pretend_ms", "server_total_ms", "shared_read_ms", "psutil_ms",
]

NODE_PORTS = {
    f"node{i}": f"http://localhost:{5000 + i}"
    for i in range(1, 11)
}


# ─────────────────────────────────────────────
#  CSV setup
# ─────────────────────────────────────────────

def init_csv():
    with open(LOG_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()


def log_row(elapsed_s, node, rtt_ms, status, network_type, bandwidth_mbps, topsis_score,
            server_pretend_ms=0.0, server_total_ms=0.0, shared_read_ms=0.0, psutil_ms=0.0):
    with open(LOG_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writerow({
            "elapsed_s":      round(elapsed_s, 2),
            "timestamp":      round(time.time(), 3),
            "node":           node,
            "rtt_ms":         round(rtt_ms, 2),
            "status":         status,
            "network_type":   network_type,
            "bandwidth_mbps": round(bandwidth_mbps, 2),
            "topsis_score":   round(topsis_score, 3),
            "server_pretend_ms": round(server_pretend_ms, 2),
            "server_total_ms": round(server_total_ms, 2),
            "shared_read_ms": round(shared_read_ms, 2),
            "psutil_ms": round(psutil_ms, 2),
        })


# ─────────────────────────────────────────────
#  LCM queries
# ─────────────────────────────────────────────

def get_active_node() -> tuple[str | None, float]:
    """Ask LCM which node is currently active and return its TOPSIS score."""
    try:
        r = requests.get(f"{LCM_URL}/app/status", timeout=3)
        r.raise_for_status()
        data = r.json()
        return data.get("active_node"), float(data.get("topsis_score", 0.0))
    except Exception:
        return None, 0.0


# ─────────────────────────────────────────────
#  Graceful shutdown
# ─────────────────────────────────────────────

_running = True

def handle_exit(sig, frame):
    global _running
    _running = False
    print("\n[Client] Shutting down. Log saved to:", LOG_PATH, flush=True)


signal.signal(signal.SIGINT, handle_exit)
signal.signal(signal.SIGTERM, handle_exit)


# ─────────────────────────────────────────────
#  Main loop
# ─────────────────────────────────────────────

def run():
    init_csv()
    start_time = time.time()
    request_num = 0

    print("[Client] Starting. Polling LCM for active node...", flush=True)
    print(f"[Client] Logging to: {LOG_PATH}", flush=True)
    print("[Client] Press Ctrl+C to stop.", flush=True)
    print("─" * 65, flush=True)

    current_node = None

    while _running:
        loop_start = time.time()
        elapsed    = loop_start - start_time
        request_num += 1

        # ── Step 1: Get active node from LCM ──
        active, topsis_score = get_active_node()
        if active is None:
            print(f"[Client] #{request_num:4d} | {elapsed:6.1f}s | LCM unreachable — waiting...", flush=True)
            log_row(elapsed, "none", 0, "lcm_unreachable", "unknown", 0.0, topsis_score)
            time.sleep(random.uniform(0.2, 3))
            continue

        # Detect migration (node changed)
        if active != current_node:
            if current_node is not None:
                print(f"[Client] ★ Node changed: {current_node} → {active}", flush=True)
            current_node = active

        # ── Step 2: Send request to active node ──
        node_url = NODE_PORTS.get(active)
        if not node_url:
            print(f"[Client] #{request_num:4d} | Unknown node: {active}", flush=True)
            time.sleep(REQUEST_INTERVAL)
            continue

        t0 = time.time()
        network_type = "unknown"
        bandwidth_mbps = 0.0
        try:
            r = requests.post(f"{node_url}/process", timeout=HTTP_TIMEOUT_S)
            rtt_ms = (time.time() - t0) * 1000
            status = "ok" if r.status_code == 200 else f"http_{r.status_code}"
            if r.status_code == 200:
                payload = r.json()
                network_type = payload.get("network_type", "unknown")
                bandwidth_mbps = float(payload.get("bandwidth_mbps", 0.0))
                server_pretend_ms = float(payload.get("server_pretend_ms", 0.0))
                server_total_ms = float(payload.get("server_total_ms", 0.0))
                shared_read_ms = float(payload.get("shared_read_ms", 0.0))
                psutil_ms = float(payload.get("psutil_ms", 0.0))
        except requests.exceptions.ConnectionError:
            rtt_ms = 0
            status = "conn_error"
        except requests.exceptions.Timeout:
            rtt_ms = HTTP_TIMEOUT_S * 1000
            status = "timeout"
        except Exception:
            rtt_ms = 0
            status = "error"

        # ── Step 3: Log ──
        # ensure server timing variables exist even on error
        try:
            server_pretend_ms
        except NameError:
            server_pretend_ms = 0.0
            server_total_ms = 0.0
            shared_read_ms = 0.0
            psutil_ms = 0.0

        log_row(
            elapsed, active, rtt_ms, status, network_type, bandwidth_mbps, topsis_score,
            server_pretend_ms=server_pretend_ms,
            server_total_ms=server_total_ms,
            shared_read_ms=shared_read_ms,
            psutil_ms=psutil_ms,
        )

        # ── Print to console ──
        marker = "★ " if status != "ok" else "  "
        print(
            f"[Client]{marker}#{request_num:4d} | "
            f"{elapsed:6.1f}s | "
            f"{active:5s} | "
            f"{rtt_ms:6.1f}ms | "
            f"{status}",
            flush=True,
        )

        # ── Step 4: Sleep until next interval ──
        elapsed_this_loop = time.time() - loop_start
        sleep_time = max(0, random.uniform(0.2, 3) - elapsed_this_loop)
        time.sleep(sleep_time)

    print(f"\n[Client] Done. {request_num} requests logged.", flush=True)


if __name__ == "__main__":
    run()
