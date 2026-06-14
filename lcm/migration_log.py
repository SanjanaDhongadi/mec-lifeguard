"""
MEC-LifeGuard — Migration Log
--------------------------------
Thread-safe in-memory store for migration events.
Events are also written to results/migration_log.csv for post-run analysis.
"""

import csv
import threading
import time
from pathlib import Path

_lock   = threading.Lock()
_events = []   # list of dicts

CSV_PATH = Path(__file__).parent.parent / "results" / "migration_log.csv"
CSV_HEADERS = [
    "timestamp", "elapsed_s", "from_node", "to_node",
    "latency_before_ms", "latency_after_ms", "score_before", "score_after",
    "network_before", "network_after", "bandwidth_before", "bandwidth_after",
    "topsis_before", "topsis_after",
]

# Ensure results directory exists
CSV_PATH.parent.mkdir(exist_ok=True)


def _ensure_header():
    """Ensure the CSV file exists and uses the current headers."""
    if not CSV_PATH.exists():
        with open(CSV_PATH, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()
        return

    with open(CSV_PATH, "r", newline="") as f:
        reader = csv.reader(f)
        existing_headers = next(reader, [])

    if existing_headers != CSV_HEADERS:
        with open(CSV_PATH, "r", newline="") as rf:
            rows = list(csv.DictReader(rf))
        with open(CSV_PATH, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()
            for row in rows:
                rewritten = {key: row.get(key, "") for key in CSV_HEADERS}
                writer.writerow(rewritten)


_ensure_header()
_start_time = time.time()


def append(
    from_node: str,
    to_node: str,
    latency_before_ms: float,
    latency_after_ms: float,
    score_before: float,
    score_after: float,
    network_before: str,
    network_after: str,
    bandwidth_before: float,
    bandwidth_after: float,
    topsis_before: float,
    topsis_after: float,
) -> dict:
    """Record a migration event. Returns the event dict."""
    event = {
        "timestamp":         round(time.time(), 3),
        "elapsed_s":         round(time.time() - _start_time, 1),
        "from_node":         from_node,
        "to_node":           to_node,
        "latency_before_ms": round(latency_before_ms, 2),
        "latency_after_ms":  round(latency_after_ms, 2),
        "score_before":      round(score_before, 2),
        "score_after":       round(score_after, 2),
        "network_before":    network_before,
        "network_after":     network_after,
        "bandwidth_before":  round(bandwidth_before, 2),
        "bandwidth_after":   round(bandwidth_after, 2),
        "topsis_before":     round(topsis_before, 3),
        "topsis_after":      round(topsis_after, 3),
    }

    with _lock:
        _events.append(event)

    with open(CSV_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writerow(event)

    return event


def get_all() -> list:
    """Return a copy of all migration events (thread-safe)."""
    with _lock:
        return list(_events)


def count() -> int:
    with _lock:
        return len(_events)
