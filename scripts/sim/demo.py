"""Live terminal telemetry monitor for the FastER emergency department simulation.

Displays a self-refreshing statistic board and continuous live scrolling event feed.
Synthesizes stochastic patient arrivals, triage flows, clinical evaluations,
boarding friction, and dynamic wait-time predictions in real time.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta
from typing import List

from .engine import ERSimulation
from .models import (
    ESILevel,
    EventType,
    HospitalStateSnapshot,
    RegionalNetworkSnapshot,
    SimulationEvent,
)
from .network import HospitalNetworkSimulation


# Terminal ANSI Colors (No emojis, clean terminal styling)
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
CYAN = "\033[96m"
WHITE = "\033[97m"
BG_RED = "\033[41m"
BG_DARK = "\033[100m"

# Regex pattern for stripping ANSI escape sequences when computing visual width
ANSI_REGEX = re.compile(r"\x1b\[[0-9;]*[mK]")


def visible_len(text: str) -> int:
    """Returns visual character length, ignoring invisible ANSI escape codes."""
    return len(ANSI_REGEX.sub("", text))


def pad(text: str, width: int, align: str = "left") -> str:
    """Pads text to width based on visible character count, preserving ANSI colors."""
    vis = visible_len(text)
    spaces = max(0, width - vis)
    if align == "right":
        return (" " * spaces) + text
    if align == "center":
        left = spaces // 2
        right = spaces - left
        return (" " * left) + text + (" " * right)
    return text + (" " * spaces)


def format_esi(esi: int) -> str:
    """Returns color-coded ESI text badge without emojis. Visual length is always exactly 13."""
    if esi == 1:
        return f"{BG_RED}{WHITE}{BOLD}ESI 1 (Resus){RESET}"
    if esi == 2:
        return f"{RED}{BOLD}ESI 2 (Emerg){RESET}"
    if esi == 3:
        return f"{YELLOW}ESI 3 (Urgnt){RESET}"
    if esi == 4:
        return f"{GREEN}ESI 4 (Less) {RESET}"
    return f"{CYAN}ESI 5 (Non-U){RESET}"


def format_crowding_label(score: float, level: str) -> str:
    if score >= 140:
        return f"{RED}{BOLD}{level.upper()} (NEDOCS: {score:.1f}){RESET}"
    if score >= 100:
        return f"{RED}{level.upper()} (NEDOCS: {score:.1f}){RESET}"
    if score >= 60:
        return f"{YELLOW}{level.upper()} (NEDOCS: {score:.1f}){RESET}"
    return f"{GREEN}{level.upper()} (NEDOCS: {score:.1f}){RESET}"


def format_event_log(event: SimulationEvent, show_hospital: bool = False) -> str:
    """Formats a single telemetry event into a clean, columnar single-line log entry."""
    ts = event.timestamp.strftime("%H:%M:%S")
    time_tag = pad(f"[{ts}]", 10)
    et = event.event_type
    pid = pad(event.patient_id or "---------", 15)
    data = event.data
    h_prefix = (pad(event.hospital_id, 14) + "  ") if show_hospital else ""

    if et == EventType.PATIENT_ARRIVED:
        tag = pad(f"{BOLD}ARRIVAL{RESET}", 14)
        esi = pad(format_esi(data.get("esi", 3)), 15)
        mode = pad("EMS" if data.get("arrival_mode") == "EMS_AMBULANCE" else "Walk-in", 7)
        complaint = pad(data.get("chief_complaint", "Unknown")[:22], 22)
        vitals = f"{RED}Abnormal{RESET}" if data.get("vitals_abnormal") else f"{GREEN}Normal{RESET}"
        age = data.get("age", 0)
        details = f"{esi} | {mode} | {complaint} | Age: {age:02d} (Vitals: {vitals})"
        return f"  {time_tag}  {tag}  {h_prefix}{pid}  {details}"

    if et == EventType.TRIAGE_STARTED:
        tag = pad(f"{CYAN}TRIAGE_START{RESET}", 14)
        nurse = data.get("nurse_id", event.staff_id or "RN")
        return f"  {time_tag}  {tag}  {h_prefix}{pid}  Intake assessment begun by {nurse}"

    if et == EventType.TRIAGE_COMPLETED:
        tag = pad(f"{CYAN}TRIAGE_DONE{RESET}", 14)
        esi = pad(format_esi(data.get("esi", 3)), 15)
        complaint = data.get("chief_complaint", "")[:24]
        return f"  {time_tag}  {tag}  {h_prefix}{pid}  Triage finalized -> {esi} | {complaint}"

    if et == EventType.BED_ASSIGNED:
        tag = pad(f"{BLUE}BED_ASSIGN{RESET}", 14)
        bed = data.get("bed_name", event.bed_id or "ER Bay")
        return f"  {time_tag}  {tag}  {h_prefix}{pid}  Transferred to {bed}"

    if et == EventType.PROVIDER_ASSIGNED:
        tag = pad(f"{GREEN}MD_SEEN{RESET}", 14)
        md = data.get("provider_name", "MD")
        wait = data.get("wait_minutes", 0.0)
        esi = format_esi(data.get("esi", 3))
        return f"  {time_tag}  {tag}  {h_prefix}{pid}  {md} started exam | Door-to-MD: {wait:.1f}m ({esi})"

    if et == EventType.DIAGNOSTICS_ORDERED:
        tag = pad(f"{YELLOW}WORKUP_ORDER{RESET}", 14)
        labs = "Labs:Yes" if data.get("requires_labs") else "Labs:No"
        img = "Imaging:Yes" if data.get("requires_imaging") else "Imaging:No"
        est = data.get("estimated_minutes", 0.0)
        return f"  {time_tag}  {tag}  {h_prefix}{pid}  Diagnostics ordered -> {labs}, {img} (Est: {est:.0f}m)"

    if et == EventType.DIAGNOSTICS_COMPLETED:
        tag = pad(f"{GREEN}WORKUP_DONE{RESET}", 14)
        return f"  {time_tag}  {tag}  {h_prefix}{pid}  Diagnostic results returned -> Awaiting physician decision"

    if et == EventType.BOARDING_STARTED:
        tag = pad(f"{RED}{BOLD}BOARDING_GRID{RESET}", 14)
        bed = event.bed_id or "ER Bay"
        return f"  {time_tag}  {tag}  {h_prefix}{pid}  Admitted but upstairs beds FULL -> Trapped in {bed}"

    if et == EventType.PATIENT_DISCHARGED:
        tag = pad(f"{GREEN}{BOLD}DISCHARGED{RESET}", 14)
        los = data.get("total_los_minutes", 0.0)
        d2md = data.get("door_to_provider_minutes", 0.0)
        return f"  {time_tag}  {tag}  {h_prefix}{pid}  Discharged Home | Total LOS: {los:.1f}m (Door-to-MD: {d2md:.1f}m)"

    if et == EventType.PATIENT_ADMITTED:
        tag = pad(f"{MAGENTA}{BOLD}WARD_ADMIT{RESET}", 14)
        board = data.get("boarding_minutes", 0.0)
        los = data.get("total_los_minutes", 0.0)
        board_str = f"BoardingWait: {board:.1f}m" if board > 0 else "Direct transfer"
        return f"  {time_tag}  {tag}  {h_prefix}{pid}  Transferred to inpatient ward | {board_str} (Total LOS: {los:.1f}m)"

    if et == EventType.PATIENT_LWBS:
        tag = pad(f"{YELLOW}{BOLD}RENEGE_LWBS{RESET}", 14)
        wait = data.get("wait_minutes", 0.0)
        esi = format_esi(data.get("esi", 4))
        return f"  {time_tag}  {tag}  {h_prefix}{pid}  Left Without Being Seen after {wait:.1f}m wait ({esi})"

    if et == EventType.INPATIENT_CAPACITY_CHANGE:
        tag = pad(f"{WHITE}WARD_UPDATE{RESET}", 14)
        avail = data.get("inpatient_beds_available", 0)
        reason = data.get("reason", "Hospital census change")
        return f"  {time_tag}  {tag}  {h_prefix}{pid}  Upstairs inpatient beds now: {avail} ({reason})"

    if et == EventType.SURGE_TRIGGERED:
        tag = pad(f"{RED}{BOLD}EMS_CLUSTER{RESET}", 14)
        cnt = data.get("count", 0)
        acuity = format_esi(data.get("acuity", 2))
        return f"  {time_tag}  {tag}  {h_prefix}{pid}  Ambulance cluster: {cnt} patients ({acuity})"

    if et == EventType.PATIENT_DIVERTED:
        tag = pad(f"{RED}{BOLD}DIVERTED{RESET}", 14)
        dest = data.get("destination_name", data.get("destination_hospital_id", "Alternate ED"))
        reason = data.get("reason", "Overcrowded")
        return f"  {time_tag}  {tag}  {h_prefix}{pid}  Ambulance diverted -> Re-routed to {dest} ({reason})"

    return f"  {time_tag}  {pad(et.value, 14)}  {h_prefix}{pid}  {data}"


def render_dashboard(
    snapshot: HospitalStateSnapshot,
    tick_count: int,
    recent_events: List[SimulationEvent],
    term_width: int = 100,
    max_log_lines: int = 12,
) -> str:
    """Builds the complete terminal frame (statistic board on top, scrolling feed below)."""
    lines: List[str] = []
    # Standard 98-character canvas width
    canvas_w = max(98, min(120, term_width))
    divider = "=" * canvas_w
    subdivider = "-" * canvas_w

    # --- Header ---
    lines.append(divider)
    header_left = f"{BOLD}{WHITE}FastER :: EMERGENCY OPERATIONS MONITOR{RESET}"
    header_right = f"{BOLD}{snapshot.name.upper()}{RESET}"
    lines.append(pad(header_left, 50) + pad(header_right, canvas_w - 50, "right"))

    sim_time_str = snapshot.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    crowding_str = format_crowding_label(snapshot.nedocs_score, snapshot.overcrowding_level)
    clock_part = f"CLOCK: {BOLD}{sim_time_str}{RESET}"
    status_part = f"STATUS: {crowding_str}"
    tick_part = f"TICK: #{tick_count}"
    lines.append(pad(clock_part, 32) + pad(status_part, 40) + pad(tick_part, canvas_w - 72, "right"))
    lines.append(divider)

    # --- Capacity & Flow Metrics Board (98-width grid) ---
    c1_w, c2_w, c3_w = 30, 31, 31
    occ_str = f"{snapshot.occupied_er_beds}/{snapshot.total_er_beds} ({snapshot.bed_utilization_pct:.1f}%)"
    clean_str = f"{snapshot.available_er_beds} avail, {snapshot.dirty_er_beds} dirty"
    md_str = f"{snapshot.on_shift_physicians} on shift"
    avail_md_str = f"{snapshot.available_physician_slots} open slots"
    rn_str = f"{snapshot.on_shift_nurses} on shift"
    
    boarder_color = RED + BOLD if snapshot.active_boarders_in_er > 0 else GREEN
    board_str = f"{boarder_color}{snapshot.active_boarders_in_er} trapped in ER{RESET}"
    upstairs_str = f"{snapshot.inpatient_beds_available} beds available"
    in_care_str = f"{snapshot.in_treatment_count} in active care"

    grid_header = pad("ER CAPACITY & OCCUPANCY", c1_w) + " | " + pad("STAFF ROSTER & WORKLOAD", c2_w) + " | " + pad("INPATIENT BOARDING STATUS", c3_w)
    grid_sep = "-" * c1_w + "-+-" + "-" * c2_w + "-+-" + "-" * c3_w
    r1 = pad(f"ER Beds:     {occ_str}", c1_w) + " | " + pad(f"Physicians:  {md_str}", c2_w) + " | " + pad(f"Boarders:    {board_str}", c3_w)
    r2 = pad(f"Turnover:    {clean_str}", c1_w) + " | " + pad(f"MD Slots:    {avail_md_str}", c2_w) + " | " + pad(f"Upstairs:    {upstairs_str}", c3_w)
    r3 = pad(f"Lobby Queue: {snapshot.waiting_room_count} waiting", c1_w) + " | " + pad(f"Nursing:     {rn_str}", c2_w) + " | " + pad(f"In Care:     {in_care_str}", c3_w)

    lines.append(grid_header)
    lines.append(grid_sep)
    lines.append(r1)
    lines.append(r2)
    lines.append(r3)
    lines.append(subdivider)

    # --- Predicted Wait Times by Triage Tier (98-width table) ---
    lines.append(pad("PREDICTED DOOR-TO-PROVIDER WAIT TIMES (CONDITIONAL PREEMPTION)", canvas_w))
    wh = pad("Triage Tier", 15) + " | " + pad("Clinical Urgency", 16) + " | " + pad("Est. Wait", 12) + " | " + pad("90% Conf. Interval", 24) + " | " + pad("Queue Ahead", 19)
    ws = "-" * 15 + "-+-" + "-" * 16 + "-+-" + "-" * 12 + "-+-" + "-" * 24 + "-+-" + "-" * 19
    lines.append(wh)
    lines.append(ws)

    tier_urgencies = {
        1: "Resuscitation",
        2: "Emergent",
        3: "Urgent",
        4: "Less Urgent",
        5: "Non-Urgent",
    }
    for esi in range(1, 6):
        pred = snapshot.predicted_wait_times.get(esi)
        if pred:
            badge = format_esi(esi)
            urg = tier_urgencies[esi]
            wait_text = f"{pred.estimated_wait_minutes:.1f} mins"
            ci_text = f"[{pred.confidence_interval_low:.1f} - {pred.confidence_interval_high:.1f}] mins"
            q_text = f"{pred.queue_depth_ahead} patients ahead"
            lines.append(pad(badge, 15) + " | " + pad(urg, 16) + " | " + pad(wait_text, 12) + " | " + pad(ci_text, 24) + " | " + pad(q_text, 19))
    lines.append(subdivider)

    # --- Top Waiting Room Queue Prioritization (98-width table) ---
    lines.append(pad("ACTIVE LOBBY QUEUE (TOP PRIORITIZED PATIENTS)", canvas_w))
    qh = pad("Pos", 4) + " | " + pad("Patient ID", 14) + " | " + pad("Acuity Tier", 15) + " | " + pad("Wait", 7) + " | " + pad("Chief Complaint", 22) + " | " + pad("Vitals", 9) + " | " + pad("Priority", 9)
    qs = "-" * 4 + "-+-" + "-" * 14 + "-+-" + "-" * 15 + "-+-" + "-" * 7 + "-+-" + "-" * 22 + "-+-" + "-" * 9 + "-+-" + "-" * 9
    lines.append(qh)
    lines.append(qs)

    if not snapshot.waiting_queue:
        lines.append(pad("  (No patients currently queued in waiting room lobby)", canvas_w))
    else:
        for idx, item in enumerate(snapshot.waiting_queue[:5], 1):
            badge = format_esi(item.esi)
            wait_str = f"{item.wait_minutes:.1f}m"
            vitals_str = f"{RED}ABNORMAL{RESET}" if item.vitals_abnormal else f"{GREEN}Normal{RESET}"
            complaint = item.chief_complaint[:22]
            score_str = f"{item.priority_score:.0f} pts"
            row = (
                pad(f"#{idx}", 4)
                + " | "
                + pad(item.patient_id, 14)
                + " | "
                + pad(badge, 15)
                + " | "
                + pad(wait_str, 7)
                + " | "
                + pad(complaint, 22)
                + " | "
                + pad(vitals_str, 9)
                + " | "
                + pad(score_str, 9)
            )
            lines.append(row)
    lines.append(divider)

    # --- Live Scrolling Event Log Feed ---
    lines.append(pad("LIVE TELEMETRY FEED [SCROLLING]", canvas_w))
    if not recent_events:
        lines.append(pad("  (Initializing telemetry stream...)", canvas_w))
    else:
        display_events = recent_events[-max_log_lines:]
        for ev in display_events:
            lines.append(format_event_log(ev))

    lines.append(divider)
    return "\n".join(lines)


def run_live_feed(
    tick_rate: float = 0.4,
    step_minutes: int = 1,
    seed: int | None = None,
    warmup_minutes: int = 120,
    max_ticks: int | None = None,
):
    """Runs continuous live telemetry loop."""
    sys.stdout.write("\033[?25l")  # Hide cursor
    sys.stdout.flush()

    sim = ERSimulation(
        hospital_id="METRO-ED-01",
        name="Metro General Emergency Center",
        er_beds_count=24,
        inpatient_beds_available=4,
        base_arrivals_per_hour=7.5,
        start_time=datetime(2026, 11, 8, 9, 0, 0),
        seed=seed,
    )

    if warmup_minutes > 0:
        sim.run_until(sim.current_time + timedelta(minutes=warmup_minutes), step_minutes=2)

    tick = 0
    try:
        while True:
            tick += 1
            sim.step(delta_minutes=step_minutes)

            snapshot = sim.get_snapshot()
            recent_events = sim.live_events.get_latest(count=20)

            try:
                term_w = shutil.get_terminal_size((100, 35)).columns
                term_h = shutil.get_terminal_size((100, 35)).lines
            except Exception:
                term_w, term_h = 100, 35

            available_log_lines = max(6, min(16, term_h - 25))

            frame = render_dashboard(
                snapshot=snapshot,
                tick_count=tick,
                recent_events=recent_events,
                term_width=term_w,
                max_log_lines=available_log_lines,
            )

            # Home cursor and write frame
            sys.stdout.write("\033[H\033[2J")
            sys.stdout.write(frame + "\n")
            sys.stdout.flush()

            if max_ticks and tick >= max_ticks:
                break

            time.sleep(tick_rate)

    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h\n")  # Restore cursor
        sys.stdout.flush()

    print(f"\n{BOLD}Telemetry feed paused. Run summary:{RESET}")
    summary = sim.metrics.get_summary()
    print(f"Total Arrivals: {summary['volume']['total_arrivals']}")
    print(f"Total Discharged: {summary['volume']['total_discharges']}")
    print(f"Total Admitted: {summary['volume']['total_admissions']}")
    print(f"Left Without Being Seen (LWBS): {summary['volume']['total_lwbs']} ({summary['volume']['lwbs_rate_pct']}%)")
    print("Door-to-Provider Wait Times:")
    for esi, stats in summary["door_to_provider_wait_minutes"].items():
        print(f"  {esi}: Median={stats['median']}m, 90th%={stats['p90']}m (n={stats['count']})")


def render_network_dashboard(
    network_snap: RegionalNetworkSnapshot,
    tick_count: int,
    recent_events: List[SimulationEvent],
    term_width: int = 114,
    max_log_lines: int = 12,
) -> str:
    """Builds multi-hospital regional comparison dashboard frame."""
    lines: List[str] = []
    canvas_w = max(114, min(140, term_width))
    divider = "=" * canvas_w
    subdivider = "-" * canvas_w

    # Header
    lines.append(divider)
    header_left = f"{BOLD}{WHITE}FastER :: REGIONAL EMERGENCY NETWORK MONITOR [SYNCHRONIZED]{RESET}"
    header_right = f"{BOLD}FACILITIES: {len(network_snap.hospitals)}{RESET}"
    lines.append(pad(header_left, 65) + pad(header_right, canvas_w - 65, "right"))

    sim_time_str = network_snap.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    div_count = len(network_snap.hospitals_on_diversion)
    div_str = f"{RED}{BOLD}{div_count} DIVERTING{RESET}" if div_count > 0 else f"{GREEN}ALL OPEN{RESET}"
    meta_line = (
        pad(f"CLOCK: {BOLD}{sim_time_str}{RESET}", 28)
        + pad(f"TOTAL QUEUE: {network_snap.total_regional_waiting} waiting", 26)
        + pad(f"BOARDING: {network_snap.total_regional_boarders} stuck", 22)
        + pad(f"EMS STATUS: {div_str}", 22)
        + pad(f"TICK: #{tick_count}", canvas_w - 98, "right")
    )
    lines.append(meta_line)
    lines.append(divider)

    # Multi-hospital routing table
    cols = [
        ("Facility Destination", 23),
        ("ER Beds", 14),
        ("Boarders", 8),
        ("Wait(E2)", 8),
        ("Wait(E3)", 8),
        ("Travel", 7),
        ("Total", 8),
        ("Overcrowding", 18),
    ]
    wh = " | ".join(pad(c[0], c[1]) for c in cols)
    ws = "-+-".join("-" * c[1] for c in cols)
    lines.append(pad("REGIONAL EMERGENCY DEPARTMENT STATUS & ROUTING COMPARISON", canvas_w))
    lines.append(wh)
    lines.append(ws)

    for rec in network_snap.routing_recommendations:
        is_best = (rec.hospital_id == network_snap.best_hospital_id)
        marker = "* " if is_best else "  "
        fac_name = marker + rec.name[:21]
        if is_best:
            fac_name = f"{GREEN}{BOLD}{fac_name}{RESET}"
        
        snap = network_snap.hospitals.get(rec.hospital_id)
        occ_str = f"{snap.occupied_er_beds}/{snap.total_er_beds} ({rec.bed_utilization_pct:.0f}%)" if snap else f"{rec.bed_utilization_pct:.0f}%"
        board_color = RED + BOLD if rec.active_boarders_in_er > 0 else GREEN
        board_text = f"{board_color}{rec.active_boarders_in_er} stuck{RESET}"

        e2_wait = snap.predicted_wait_times[2].estimated_wait_minutes if snap and 2 in snap.predicted_wait_times else 5.0
        e3_wait = snap.predicted_wait_times[3].estimated_wait_minutes if snap and 3 in snap.predicted_wait_times else 20.0

        crowd_text = format_crowding_label(rec.nedocs_score, rec.overcrowding_level)

        row_vals = [
            fac_name,
            occ_str,
            board_text,
            f"{e2_wait:.1f}m",
            f"{e3_wait:.1f}m",
            f"{rec.travel_minutes:.0f}m",
            f"{BOLD}{rec.total_minutes:.1f}m{RESET}" if is_best else f"{rec.total_minutes:.1f}m",
            crowd_text,
        ]
        line = " | ".join(pad(row_vals[i], cols[i][1]) for i in range(len(cols)))
        lines.append(line)

    lines.append(subdivider)
    best_name = (
        network_snap.hospitals[network_snap.best_hospital_id].name
        if network_snap.best_hospital_id and network_snap.best_hospital_id in network_snap.hospitals
        else "None"
    )
    rec_line = f"* RECOMMENDED DESTINATION FOR GENERAL TRIAGE: {BOLD}{best_name}{RESET}"
    lines.append(pad(rec_line, canvas_w))
    lines.append(divider)

    # Live Event Feed across all facilities
    lines.append(pad("LIVE REGIONAL TELEMETRY FEED [ALL FACILITIES SCROLLING]", canvas_w))
    if not recent_events:
        lines.append(pad("  (Initializing regional telemetry stream...)", canvas_w))
    else:
        display_events = recent_events[-max_log_lines:]
        for ev in display_events:
            lines.append(format_event_log(ev, show_hospital=True))

    lines.append(divider)
    return "\n".join(lines)


def run_network_live_feed(
    tick_rate: float = 0.4,
    step_minutes: int = 1,
    seed: int | None = None,
    warmup_minutes: int = 120,
    max_ticks: int | None = None,
):
    """Runs continuous multi-hospital regional network telemetry loop."""
    sys.stdout.write("\033[?25l")  # Hide cursor
    sys.stdout.flush()

    network = HospitalNetworkSimulation.create_default_network(seed=seed or 42)

    if warmup_minutes > 0:
        network.run_until(network.current_time + timedelta(minutes=warmup_minutes), step_minutes=2)

    tick = 0
    try:
        while True:
            tick += 1
            network.step(delta_minutes=step_minutes)

            snapshot = network.get_network_snapshot()
            recent_events = network.unified_live_events.get_latest(count=20)

            try:
                term_w = shutil.get_terminal_size((114, 35)).columns
                term_h = shutil.get_terminal_size((114, 35)).lines
            except Exception:
                term_w, term_h = 114, 35

            available_log_lines = max(6, min(16, term_h - 22))

            frame = render_network_dashboard(
                network_snap=snapshot,
                tick_count=tick,
                recent_events=recent_events,
                term_width=term_w,
                max_log_lines=available_log_lines,
            )

            sys.stdout.write("\033[H\033[2J")
            sys.stdout.write(frame + "\n")
            sys.stdout.flush()

            if max_ticks and tick >= max_ticks:
                break

            time.sleep(tick_rate)

    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h\n")  # Restore cursor
        sys.stdout.flush()

    print(f"\n{BOLD}Regional network feed paused. Aggregated summary:{RESET}")
    summary = network.unified_metrics.get_summary()
    print(f"Total Regional Arrivals: {summary['volume']['total_arrivals']}")
    print(f"Total Regional Discharged: {summary['volume']['total_discharges']}")
    print(f"Total Regional Admitted: {summary['volume']['total_admissions']}")
    print(f"Left Without Being Seen: {summary['volume']['total_lwbs']} ({summary['volume']['lwbs_rate_pct']}%)")


def main():
    parser = argparse.ArgumentParser(description="FastER Live Emergency Department Feed")
    parser.add_argument("--network", action="store_true", help="Simulate a 3-hospital regional network with EMS diversion")
    parser.add_argument("--tick-rate", type=float, default=0.4, help="Seconds between ticks (default: 0.4s)")
    parser.add_argument("--step-minutes", type=int, default=1, help="Simulation minutes advanced per tick (default: 1)")
    parser.add_argument("--seed", type=int, default=None, help="Random seed (default: random)")
    parser.add_argument("--ticks", type=int, default=None, help="Stop after N ticks (for headless runs)")
    parser.add_argument("--no-warmup", action="store_true", help="Start from 0:00 without warmup")
    args = parser.parse_args()

    warmup = 0 if args.no_warmup else 120
    if args.network:
        run_network_live_feed(
            tick_rate=args.tick_rate,
            step_minutes=args.step_minutes,
            seed=args.seed,
            warmup_minutes=warmup,
            max_ticks=args.ticks,
        )
    else:
        run_live_feed(
            tick_rate=args.tick_rate,
            step_minutes=args.step_minutes,
            seed=args.seed,
            warmup_minutes=warmup,
            max_ticks=args.ticks,
        )


if __name__ == "__main__":
    main()
