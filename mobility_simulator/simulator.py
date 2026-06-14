"""
MEC-LifeGuard — Mobility Simulator
-------------------------------------
Simulates a mobile user moving through 10 edge node coverage zones.
Each zone updates network, RTT, CPU, memory, and failure state.
The edge node Flask apps read this file before every response so the
LCM and client observe a realistic dynamic environment.

CHANGES (task 2 & 3):
  - Infinite loop: simulation never stops, re-shuffles zones each round
  - Random zone order every round
  - Random zone duration per visit
  - Bluetooth properly weighted — bandwidth penalty applied in RTT noise
  - Network transitions are random, not round-robin

Run: python mobility_simulator/simulator.py
"""

import json
import random
import time
import signal
import sys
from pathlib import Path

NODE_IDS = [f"node{i}" for i in range(1, 11)]

NETWORK_PROFILES = {
    "Bluetooth": {
        "latency_range":   [80, 150],   # high latency
        "bandwidth_range": [1, 5],      # very low bandwidth
        "weight":          0.10,        # low selection probability — bad network
    },
    "4G": {
        "latency_range":   [30, 80],
        "bandwidth_range": [20, 100],
        "weight":          0.30,
    },
    "5G": {
        "latency_range":   [5, 20],
        "bandwidth_range": [100, 1000],
        "weight":          0.40,
    },
    "6G": {
        "latency_range":   [1, 5],
        "bandwidth_range": [1000, 10000],
        "weight":          0.20,
    },
}

STATE_FILE      = Path(__file__).parent / "mobility_state.json"
UPDATE_INTERVAL_S = 5

NODE_STATE: dict[str, dict] = {
    node_id: {
        "cpu_pct":   random.uniform(10, 40),
        "memory_mb": random.uniform(500, 900),
        "failed":    False,
    }
    for node_id in NODE_IDS
}


def _build_zone(node_index: int) -> dict:
    zone_name = chr(ord("A") + node_index)
    base_rtt = {}
    for idx, node_id in enumerate(NODE_IDS):
        distance = abs(idx - node_index)
        if distance == 0:
            base_rtt[node_id] = 10
        elif distance == 1:
            base_rtt[node_id] = 25
        elif distance == 2:
            base_rtt[node_id] = 40
        elif distance == 3:
            base_rtt[node_id] = 60
        else:
            base_rtt[node_id] = 80 + (distance - 3) * 15
    return {
        "name":        zone_name,
        "description": f"User near {NODE_IDS[node_index]} coverage area",
        "base_rtt":    base_rtt,
    }

ZONES = [_build_zone(i) for i in range(len(NODE_IDS))]


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def choose_network_profile() -> tuple[str, float]:
    """Pick a wireless profile using weighted random — Bluetooth is rare and penalised."""
    population = list(NETWORK_PROFILES.keys())
    weights    = [NETWORK_PROFILES[p]["weight"] for p in population]
    profile    = random.choices(population, weights=weights, k=1)[0]
    bandwidth  = random.uniform(*NETWORK_PROFILES[profile]["bandwidth_range"])
    return profile, round(bandwidth, 2)


def write_state(zone: dict, elapsed_in_zone: float, network_type: str, bandwidth_mbps: float) -> None:
    state = {
        "network_type":   network_type,
        "bandwidth_mbps": round(bandwidth_mbps, 2),
        "current_zone":   zone["name"],
        "zone_elapsed_s": round(elapsed_in_zone, 1),
    }
    for node_id in NODE_IDS:
        ns = NODE_STATE[node_id]
        state[node_id] = {
            "rtt_ms":    round(ns["rtt_ms"], 1),
            "cpu_pct":   round(ns["cpu_pct"], 2),
            "memory_mb": round(ns["memory_mb"], 2),
            "failed":    ns["failed"],
        }
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


def update_node_state(zone: dict, network: str, bandwidth_mbps: float) -> None:
    """Simulate node CPU, memory, RTT, and failure/recovery dynamics.
    Bluetooth adds a heavy bandwidth-penalty to RTT so TOPSIS scores it poorly."""
    for node_id in NODE_IDS:
        ns     = NODE_STATE[node_id]
        failed = ns["failed"]

        if failed:
            ns["failed"] = random.random() < 0.10
            if ns["failed"]:
                ns["rtt_ms"]  = 999.0
                ns["cpu_pct"] = 100.0
            else:
                ns["cpu_pct"] = clamp(ns["cpu_pct"], 5, 100)
        else:
            if random.random() < 0.005:
                ns["failed"]  = True
                ns["cpu_pct"] = 100.0
                ns["rtt_ms"]  = 999.0
            else:
                drift = random.uniform(-3.0, 3.0)
                ns["cpu_pct"] = clamp(ns["cpu_pct"] + drift, 5.0, 100.0)
                if random.random() < 0.1:
                    ns["cpu_pct"] = clamp(ns["cpu_pct"] + random.uniform(30.0, 50.0), 5.0, 100.0)
                ns["memory_mb"] = clamp(
                    ns["memory_mb"] + random.uniform(-10.0, 10.0),
                    256.0, 1600.0,
                )

        baseline_rtt   = zone["base_rtt"][node_id]
        noise          = random.gauss(0, 5)
        profile_latency = random.uniform(*NETWORK_PROFILES[network]["latency_range"])

        # Bluetooth penalty: very low bandwidth causes buffering — adds extra RTT
        if network == "Bluetooth":
            bandwidth_penalty = max(0, (5.0 - bandwidth_mbps) * 10)  # up to +50ms
        else:
            bandwidth_penalty = 0.0

        ns["rtt_ms"] = clamp(
            baseline_rtt + noise + profile_latency * 0.3 + bandwidth_penalty,
            1.0, 999.0,
        )

        if ns["failed"]:
            ns["rtt_ms"] = 999.0


def handle_exit(sig, frame):
    print("\n[Simulator] Shutting down gracefully.", flush=True)
    sys.exit(0)

signal.signal(signal.SIGINT,  handle_exit)
signal.signal(signal.SIGTERM, handle_exit)


def run_round(round_num: int) -> None:
    """Run one full pass through all zones in random order."""
    zone_order = ZONES.copy()
    random.shuffle(zone_order)                       # ← random order every round

    print(f"\n[Simulator] ══ Round {round_num} ══  Zone order: {' → '.join(z['name'] for z in zone_order)}", flush=True)

    for zone in zone_order:
        duration      = random.uniform(10, 30)       # ← random duration per zone visit
        network_type, bandwidth_mbps = choose_network_profile()

        zone_start = time.time()
        zone_end   = zone_start + duration

        print(f"\n[Simulator] ▶ Zone {zone['name']}: {zone['description']}", flush=True)
        print(f"[Simulator]   Network={network_type} @ {bandwidth_mbps:.1f} Mbps  duration={duration:.0f}s", flush=True)

        elapsed_in_zone = 0.0
        update_node_state(zone, network_type, bandwidth_mbps)
        write_state(zone, elapsed_in_zone, network_type, bandwidth_mbps)

        while time.time() < zone_end:
            elapsed_in_zone = time.time() - zone_start
            remaining       = zone_end - time.time()

            # 35% chance to switch network mid-zone (simulate moving between cells)
            prev_network = network_type
            if random.random() < 0.35:
                network_type, bandwidth_mbps = choose_network_profile()

            if network_type != prev_network:
                print(f"[Simulator]   Network SWITCH: {prev_network} → {network_type} @ {bandwidth_mbps:.1f} Mbps", flush=True)

            update_node_state(zone, network_type, bandwidth_mbps)
            write_state(zone, elapsed_in_zone, network_type, bandwidth_mbps)

            print(
                f"[Simulator] Zone {zone['name']} | "
                f"{elapsed_in_zone:.0f}s/{duration:.0f}s | "
                f"{network_type} | {bandwidth_mbps:.1f} Mbps",
                flush=True,
            )

            time.sleep(min(UPDATE_INTERVAL_S, max(1, remaining)))


if __name__ == "__main__":
    print("[Simulator] Starting infinite mobility simulation (Ctrl+C to stop)", flush=True)
    print(f"[Simulator] State file: {STATE_FILE}", flush=True)
    print("─" * 60, flush=True)
    round_num = 1
    while True:                                      # ← infinite loop
        run_round(round_num)
        round_num += 1
        print(f"\n[Simulator] Round {round_num - 1} complete — reshuffling zones...\n", flush=True)
        time.sleep(2)
