"""
MEC-LifeGuard — Results Plotter
----------------------------------
Reads latency_log.csv (from client) and migration_log.csv (from LCM).
Generates two publication-quality PNG graphs:
  1. latency_timeseries.png  — RTT over time with migration event markers
  2. node_scores.png         — Average RTT comparison across all edge nodes

Run after the simulation: python dashboard/plot_results.py
"""

import sys
from pathlib import Path

import pandas as pd
import matplotlib
matplotlib.use("Agg")   # Non-interactive backend (works in WSL2 without display)
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

# ─────────────────────────────────────────────
#  Paths
# ─────────────────────────────────────────────

ROOT         = Path(__file__).parent.parent
LAT_CSV      = ROOT / "client" / "latency_log.csv"
MIG_CSV      = ROOT / "results" / "migration_log.csv"
OUT_DIR      = ROOT / "results"
OUT_DIR.mkdir(exist_ok=True)

NODE_COLORS = {
    "node1": "#4C9EE8",
    "node2": "#F4845F",
    "node3": "#6EC97F",
    "node4": "#9B51E0",
    "node5": "#E0A800",
    "node6": "#00B9A0",
    "node7": "#D23F57",
    "node8": "#4854DB",
    "node9": "#2D9CDB",
    "node10": "#B04632",
    "none":  "#AAAAAA",
}


# ─────────────────────────────────────────────
#  Load data
# ─────────────────────────────────────────────

def load_latency() -> pd.DataFrame:
    if not LAT_CSV.exists():
        print(f"[Plotter] ERROR: {LAT_CSV} not found. Run client.py first.", flush=True)
        sys.exit(1)
    df = pd.read_csv(LAT_CSV)
    df = df[df["status"] == "ok"]   # only successful requests
    return df


def load_migrations() -> pd.DataFrame:
    if not MIG_CSV.exists() or MIG_CSV.stat().st_size < 10:
        print("[Plotter] No migration events found (migration_log.csv empty).", flush=True)
        return pd.DataFrame(columns=["elapsed_s", "from_node", "to_node",
                                     "latency_before_ms", "latency_after_ms"])
    return pd.read_csv(MIG_CSV)


# ─────────────────────────────────────────────
#  Plot 1: Latency Timeseries
# ─────────────────────────────────────────────

def plot_latency_timeseries(df: pd.DataFrame, mig: pd.DataFrame):
    fig, ax = plt.subplots(figsize=(14, 6))

    # Plot RTT per node (different colour per node)
    for node_id, group in df.groupby("node"):
        color = NODE_COLORS.get(node_id, "#888888")
        ax.plot(
            group["elapsed_s"],
            group["rtt_ms"],
            color=color,
            linewidth=1.5,
            alpha=0.85,
            label=node_id,
        )

    # Draw migration markers
    for _, ev in mig.iterrows():
        t = ev["elapsed_s"]
        ax.axvline(x=t, color="red", linestyle="--", linewidth=1.2, alpha=0.8)
        ax.annotate(
            f"Migration\n{ev['from_node']}→{ev['to_node']}\n↓{ev['latency_before_ms']:.0f}→{ev['latency_after_ms']:.0f}ms",
            xy=(t, ax.get_ylim()[1] if ax.get_ylim()[1] > 0 else 100),
            xytext=(t + 1, 85),
            fontsize=7.5,
            color="red",
            arrowprops=dict(arrowstyle="->", color="red", lw=0.8),
        )

    # SLA threshold line
    ax.axhline(y=60, color="orange", linestyle=":", linewidth=1.5, label="SLA threshold (60ms)")

    ax.set_xlabel("Elapsed Time (seconds)", fontsize=12)
    ax.set_ylabel("Measured RTT (ms)", fontsize=12)
    ax.set_title("MEC-LifeGuard — Latency Timeseries with Migration Events", fontsize=14, fontweight="bold")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, linestyle="--", alpha=0.4)
    ax.set_ylim(bottom=0)

    # Zone shading (approximate 60s zones)
    zone_colors = ["#EEF4FF", "#FFF4EE", "#EEFFF2"]
    zone_labels = ["Zone A\n(User near Node 1)", "Zone B\n(User near Node 2)", "Zone C\n(User near Node 3)"]
    for i, (color, label) in enumerate(zip(zone_colors, zone_labels)):
        ax.axvspan(i * 60, (i + 1) * 60, alpha=0.3, color=color)
        ax.text(i * 60 + 5, ax.get_ylim()[1] * 0.95 if ax.get_ylim()[1] > 0 else 95,
                label, fontsize=8, color="#555555")

    plt.tight_layout()
    out = OUT_DIR / "latency_timeseries.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"[Plotter] Saved: {out}", flush=True)


# ─────────────────────────────────────────────
#  Plot 2: Node Score Comparison by Zone
# ─────────────────────────────────────────────

def plot_node_scores(df: pd.DataFrame):
    """
    Plot the average RTT per node across the simulation run.
    """
    df = df.copy()
    nodes = sorted(df["node"].unique())
    averages = df.groupby("node")["rtt_ms"].mean().reindex(nodes).fillna(0)

    fig, ax = plt.subplots(figsize=(12, 6))

    bars = ax.bar(
        nodes,
        averages,
        color=[NODE_COLORS.get(node_id, "#888888") for node_id in nodes],
        alpha=0.85,
    )

    for bar, val in zip(bars, averages):
        if val > 0:
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 1.5,
                f"{val:.0f}ms",
                ha="center",
                va="bottom",
                fontsize=8,
            )

    ax.set_xlabel("Edge Node", fontsize=12)
    ax.set_ylabel("Average RTT (ms)", fontsize=12)
    ax.set_title("MEC-LifeGuard — Average RTT by Edge Node", fontsize=13, fontweight="bold")
    ax.grid(True, axis="y", linestyle="--", alpha=0.4)
    ax.set_ylim(bottom=0)

    plt.tight_layout()
    out = OUT_DIR / "node_scores.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"[Plotter] Saved: {out}", flush=True)


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("[Plotter] Loading data...", flush=True)
    lat_df = load_latency()
    mig_df = load_migrations()

    print(f"[Plotter] {len(lat_df)} successful requests, {len(mig_df)} migrations", flush=True)

    plot_latency_timeseries(lat_df, mig_df)
    plot_node_scores(lat_df)

    print("\n[Plotter] ✓ Both graphs saved to results/", flush=True)
    print("  • results/latency_timeseries.png", flush=True)
    print("  • results/node_scores.png", flush=True)
