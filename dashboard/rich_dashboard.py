"""
MEC-LifeGuard — Live Rich Dashboard
--------------------------------------
Polls the LCM every 2 seconds and renders a live terminal table showing:
  • All 3 nodes: RTT, CPU, score, status, active marker
  • Current active node and uptime
  • Migration count and recent migration log

Run: python dashboard/rich_dashboard.py
"""

import time
import signal
import sys

import requests
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich import box

LCM_URL    = "http://localhost:8000"
POLL_DELAY = 2  # seconds between refreshes

console = Console()
_running = True

def handle_exit(sig, frame):
    global _running
    _running = False

signal.signal(signal.SIGINT, handle_exit)
signal.signal(signal.SIGTERM, handle_exit)


def fetch(path: str) -> dict | None:
    try:
        r = requests.get(f"{LCM_URL}{path}", timeout=3)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def score_color(score: float) -> str:
    if score >= 0.7:   return "green"
    if score >= 0.4:   return "yellow"
    return "red"


def build_layout() -> Panel:
    # ── Fetch data ──────────────────────────────────────
    nodes_data  = fetch("/nodes")
    status_data = fetch("/app/status")
    mig_data    = fetch("/migrations")

    elapsed = "–"
    active  = "–"
    mig_cnt = 0

    if status_data:
        active  = status_data.get("active_node") or "none"
        elapsed = f"{status_data.get('uptime_s', 0):.0f}s"
        mig_cnt = status_data.get("migration_count", 0)

    # ── Node table ───────────────────────────────────────
    node_table = Table(
        title="Edge Nodes",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        expand=True,
    )
    node_table.add_column("Node",    style="bold white",  width=8)
    node_table.add_column("Health",  width=10)
    node_table.add_column("Network", width=12)
    node_table.add_column("Bandwidth", justify="right", width=12)
    node_table.add_column("Memory", justify="right", width=10)
    node_table.add_column("RTT",     justify="right", width=10)
    node_table.add_column("CPU %",   justify="right", width=8)
    node_table.add_column("TOPSIS",  justify="right", width=10)
    node_table.add_column("Active",  justify="center", width=8)

    network_styles = {
        "Bluetooth": "magenta",
        "4G": "cyan",
        "5G": "green",
        "6G": "bright_yellow",
    }

    if nodes_data:
        for node in nodes_data.get("nodes", []):
            nid          = node.get("id", "?")
            status       = node.get("status", "unknown")
            network      = node.get("network_type", "unknown")
            bandwidth    = node.get("bandwidth_mbps", 0)
            memory       = node.get("memory_mb", 0)
            rtt          = node.get("rtt_ms", 0)
            cpu          = node.get("cpu_pct", 0)
            topsis       = node.get("score", 0)
            is_act       = node.get("active", False)
            failed       = node.get("failed", False)

            network_style = network_styles.get(network, "white")
            network_text = Text(network, style=f"bold {network_style}")
            health_text = Text("✓ healthy", style="green") if not failed else Text("✗ failed", style="bold red")
            act_txt = Text("▶  YES", style="bold green") if is_act else Text("–", style="dim")
            st_style = "green" if status == "ready" else ("yellow" if status == "idle" else "red")

            node_table.add_row(
                nid,
                health_text,
                network_text,
                f"{bandwidth:.1f} Mbps",
                f"{memory:.0f} MB",
                f"{rtt:.1f} ms",
                f"{cpu:.1f}",
                Text(f"{topsis:.3f}", style=score_color(topsis)),
                act_txt,
            )
    else:
        node_table.add_row("[red]LCM unreachable[/red]", "", "", "", "", "")

    # ── Migration log ────────────────────────────────────
    mig_table = Table(
        title=f"Migration Log  ({mig_cnt} total)",
        box=box.SIMPLE,
        show_header=True,
        header_style="bold magenta",
        expand=True,
    )
    mig_table.add_column("Time",    width=8)
    mig_table.add_column("From",    width=8)
    mig_table.add_column("To",      width=8)
    mig_table.add_column("RTT Before", justify="right", width=12)
    mig_table.add_column("RTT After",  justify="right", width=12)

    if mig_data and mig_data.get("events"):
        # Show last 5 migrations
        for ev in mig_data["events"][-5:]:
            mig_table.add_row(
                f"{ev.get('elapsed_s', 0):.0f}s",
                ev.get("from_node", "?"),
                ev.get("to_node", "?"),
                f"{ev.get('latency_before_ms', 0):.1f} ms",
                Text(f"{ev.get('latency_after_ms', 0):.1f} ms", style="green"),
            )
    else:
        mig_table.add_row("–", "–", "–", "–", "–")

    # ── Status bar ───────────────────────────────────────
    status_text = Text.assemble(
        ("  MEC-LifeGuard  ", "bold white on blue"),
        "  Active: ",
        (active, "bold green"),
        "   Uptime: ",
        (elapsed, "cyan"),
        "   Migrations: ",
        (str(mig_cnt), "bold yellow"),
        "   [dim](Ctrl+C to exit)[/dim]",
    )

    layout = Panel(
        Columns([node_table, mig_table], expand=True),
        title=status_text,
        border_style="bright_blue",
        padding=(1, 2),
    )
    return layout


def run():
    console.print("\n[bold cyan]MEC-LifeGuard Live Dashboard[/bold cyan]")
    console.print("Connecting to LCM at", LCM_URL, "\n")

    with Live(build_layout(), refresh_per_second=0.5, console=console) as live:
        while _running:
            time.sleep(POLL_DELAY)
            live.update(build_layout())

    console.print("\n[yellow]Dashboard stopped.[/yellow]")


if __name__ == "__main__":
    run()
