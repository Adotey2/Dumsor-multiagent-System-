"""
simulation.py
=============
Dumsor Guard — 7-Day XMPP Simulation

XMPP Server : xmpp.jp (public — no local Prosody needed)
Agents      : dumsor_guard@xmpp.jp      / newagent1
              dumsor_notifier@xmpp.jp   / newagent2

Day schedule
------------
  Day 1  Monday     Plan A only      No outages — baseline
  Day 2  Tuesday    Plan B x2        Two unannounced cuts
  Day 3  Wednesday  Plan C x1        One scheduled cut
  Day 4  Thursday   Plan B → Plan D  Fuel goes critical
  Day 5  Friday     Refuel + Plan C  Recovery after alert
  Day 6  Saturday   Plan B (night)   Generator withheld
  Day 7  Sunday     Plan B → Plan D  End-of-week depletion
"""

import asyncio
import sys
from datetime import datetime

import spade

from environment import Environment
from dumsor_agent import DumsorAgent, log
from notifier_agent import NotifierAgent


# ---------------------------------------------------------------------------
# XMPP credentials — xmpp.jp public server (no local Prosody needed)
# ---------------------------------------------------------------------------

XMPP_SERVER   = "xmpp.jp"
DUMSOR_JID    = "dumsor_guard@xmpp.jp"
DUMSOR_PASS   = "newagent1"
NOTIFIER_JID  = "dumsor_notifier@xmpp.jp"
NOTIFIER_PASS = "newagent2"


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def day_header(num: int, name: str, theme: str):
    print(f"\n{'='*62}")
    print(f"  DAY {num} — {name.upper()}")
    print(f"  {theme}")
    print(f"{'='*62}")


def outage_header(num: int, day: int, plan: str, desc: str):
    print(f"\n  {'─'*58}")
    print(f"  DAY {day} | OUTAGE {num}/2 | {plan}")
    print(f"  {desc}")
    print(f"  {'─'*58}")


def fuel_report(env: Environment, label: str = ""):
    bar = int(env.fuel_pct // 10)
    print(f"\n  ⛽  FUEL STATUS{(' — ' + label) if label else ''}")
    print(f"      Tank : {'▓'*bar}{'░'*(10-bar)}  "
          f"{env.fuel_litres:.2f}L  ({env.fuel_pct:.1f}%)")
    print(f"      Consumed this week : {env.fuel_consumed_L:.2f}L")
    print(f"      Cost so far        : ₵{env.total_cost_GHS:.2f}")
    print(f"      Est. runtime left  : ~{env.fuel_litres:.1f}hrs")
    print(f"      ↳ carries into next scenario\n")


def daily_summary(env: Environment, day: int, name: str,
                  outages: int, gen_today: int, cost_today: float):
    print(f"\n  ┌{'─'*58}┐")
    print(f"  │  END OF DAY {day} — {name:<45}│")
    print(f"  │  Outages today    : {outages:<39}│")
    print(f"  │  Generator starts : {gen_today:<39}│")
    print(f"  │  Cost today       : ₵{cost_today:<38.2f}│")
    print(f"  │  Fuel remaining   : {env.fuel_pct:.1f}%  "
          f"({env.fuel_litres:.2f}L){'':>29}│")
    print(f"  └{'─'*58}┘")


# ---------------------------------------------------------------------------
# Outage runner
# ---------------------------------------------------------------------------

async def run_outage(env: Environment,
                     hour: int, announced: bool,
                     duration_hrs: float,
                     reaction_wait: float = 6.0) -> float:
    fuel_before   = env.fuel_pct
    litres_before = env.fuel_litres
    cost_before   = env.total_cost_GHS
    waking        = 6 <= hour < 23

    print(f"\n  ┌─ BEFORE {'─'*49}┐")
    print(f"  │  Time        : {hour:02d}:00   "
          f"Waking hours : {'YES' if waking else 'NO (night)  '}{'':<18}│")
    print(f"  │  Fuel in tank: {litres_before:.2f}L  ({fuel_before:.1f}%){'':<28}│")
    print(f"  │  Cost to date: ₵{cost_before:.2f}{'':<44}│")
    print(f"  └{'─'*58}┘")
    env.print_appliances("BEFORE — all appliances powered by ECG")

    env.sim_time = env.sim_time.replace(hour=hour, minute=0)
    env.trigger_outage(announced=announced)
    log("SIMULATION",
        f"{'Scheduled' if announced else 'Unannounced'} cut at "
        f"{hour:02d}:00 | Expected: {duration_hrs:.1f}hrs | Waking: {waking}",
        "CRIT" if not announced else "WARN")

    log("SIMULATION",
        f"Waiting {reaction_wait:.0f}s for SPADE behaviours to fire...", "INFO")
    await asyncio.sleep(reaction_wait)

    gen_started = env.generator_running
    if gen_started:
        env.print_appliances("DURING — generator ON, non-critical loads SHED")
        litres_will_use = env.burn_rate_Lph * duration_hrs
        cost_est = litres_will_use * env.fuel_price_GHS
        print(f"  ▶ Generator running:")
        print(f"    ├ Burn rate : {env.burn_rate_Lph:.1f}L/hr  ×  {duration_hrs:.1f}hrs")
        print(f"    ├ Fuel use  : {litres_will_use:.2f}L")
        print(f"    ├ Fuel after: "
              f"{max(0.0, litres_before - litres_will_use):.2f}L  "
              f"({max(0.0, fuel_before - litres_will_use/env.fuel_capacity_L*100):.1f}%)")
        print(f"    └ This cut  : ₵{cost_est:.2f}")
    else:
        reason = "night hours" if not waking else "fuel critical (Plan D)"
        env.print_appliances(
            f"DURING — generator WITHHELD ({reason}), loads shed")
        print(f"  · Generator WITHHELD ({reason})")
        print(f"    ├ Critical loads (Fridge, Router) : backup only")
        print(f"    └ Fuel preserved : {env.fuel_litres:.2f}L — unchanged")

    env.advance_time(duration_hrs)
    litres_used = (fuel_before - env.fuel_pct) / 100.0 * env.fuel_capacity_L
    cost_this   = env.total_cost_GHS - cost_before

    bar_b = int(fuel_before  // 10)
    bar_a = int(env.fuel_pct // 10)
    print(f"\n  ┌─ FUEL CONSUMED ({duration_hrs:.1f}hrs) {'─'*35}┐")
    if litres_used > 0.01:
        print(f"  │  Before : {'▓'*bar_b}{'░'*(10-bar_b)}  "
              f"{litres_before:.2f}L  ({fuel_before:.1f}%){'':<12}│")
        print(f"  │  Used   : {litres_used:.2f}L consumed  "
              f"cost: ₵{cost_this:.2f}{'':<22}│")
        print(f"  │  After  : {'▓'*bar_a}{'░'*(10-bar_a)}  "
              f"{env.fuel_litres:.2f}L  ({env.fuel_pct:.1f}%)  "
              f"← CARRIES FORWARD │")
    else:
        print(f"  │  Generator NOT started — 0.00L — ₵0.00{'':<18}│")
        print(f"  │  Tank   : {'▓'*bar_a}{'░'*(10-bar_a)}  "
              f"{env.fuel_litres:.2f}L  ({env.fuel_pct:.1f}%)  "
              f"← CARRIES FORWARD │")
    print(f"  │  Week total : {env.fuel_consumed_L:.2f}L used  "
          f"₵{env.total_cost_GHS:.2f} total{'':<16}│")
    print(f"  └{'─'*58}┘")

    env.restore_power()
    log("SIMULATION", "ECG power restored — waiting for agent clean-up...", "OK")
    await asyncio.sleep(reaction_wait)

    env.print_appliances("AFTER — ECG restored, all loads back ON")
    fuel_report(env)
    return cost_this


# ---------------------------------------------------------------------------
# Main 7-day simulation
# ---------------------------------------------------------------------------

async def run_simulation():

    print("\n" + "="*62)
    print("  DUMSOR GUARD — 7-DAY WEEKLY SIMULATION")
    print("  Intelligent Home Power Management Agent")
    print("  Prometheus Methodology | SPADE Multi-Agent System")
    print(f"  XMPP Server : {XMPP_SERVER}  (public — no local setup needed)")
    print("  Simulated household — Accra, Ghana")
    print("="*62)
    print(f"  Agent 1 : {DUMSOR_JID}")
    print(f"  Agent 2 : {NOTIFIER_JID}")
    print("  Fuel    : 10.0L (100% at start) | ₵14.50/litre | 1.0L/hr")
    print("  Plan D  : fuel below 20%")
    print("  Night   : 23:00–06:00 (generator withheld)")
    print("="*62)

    env = Environment(
        fuel_pct=100.0, start_hour=6,
        fuel_capacity_L=10.0, burn_rate_Lph=1.0,
        fuel_price_GHS=14.5
    )
    env.add_scheduled_cut(start_hour=14, end_hour=18)

    # ── Connect to xmpp.jp ────────────────────────────────────────────────
    # xmpp.jp has a valid signed TLS certificate so verify_security=True.
    # auto_register=False because accounts already exist on xmpp.jp.
    print(f"\n  Connecting agents to {XMPP_SERVER}...")
    notifier = NotifierAgent(
        jid=NOTIFIER_JID,
        password=NOTIFIER_PASS,
        verify_security=True          # xmpp.jp has a real TLS cert
    )
    agent = DumsorAgent(
        jid=DUMSOR_JID,
        password=DUMSOR_PASS,
        environment=env,
        notifier_jid=NOTIFIER_JID,
        verify_security=True          # xmpp.jp has a real TLS cert
    )

    await notifier.start(auto_register=False)  # accounts pre-registered on xmpp.jp
    await agent.start(auto_register=False)
    print(f"  ✓ Both agents connected to {XMPP_SERVER} — behaviours running\n")

    await asyncio.sleep(3)

    week_summary = []

    # ═══════════════════════════════════════════════════════════════════════
    # DAY 1 — MONDAY
    # ═══════════════════════════════════════════════════════════════════════
    day_header(1, "Monday", "Plan A — Continuous monitoring, no outages today")
    print("  ECG power is stable. No cuts occur.")
    print("  Plan A runs every 3s — reads percepts, updates beliefs,")
    print("  checks ECG schedule, estimates outage probability.\n")
    env.sim_time = env.sim_time.replace(hour=6, minute=0)
    log("SIMULATION", "Day 1: letting Plan A run for 15s...", "INFO")
    await asyncio.sleep(15)
    env.print_status()
    fuel_report(env, "end of Day 1 — no generator use, full tank")
    daily_summary(env, 1, "Monday", outages=0, gen_today=0, cost_today=0.0)
    week_summary.append({"day":1,"name":"Monday","plan":"A",
                         "outages":0,"gen_starts":0,
                         "cost":0.0,"fuel_end":env.fuel_pct})

    # ═══════════════════════════════════════════════════════════════════════
    # DAY 2 — TUESDAY
    # ═══════════════════════════════════════════════════════════════════════
    day_header(2, "Tuesday", "Plan B x2 — Two unannounced cuts, waking hours")
    gen_before  = env.generator_starts
    cost_before = env.total_cost_GHS
    outage_header(1, 2, "Plan B", "Unannounced cut 08:00 — waking hours, fuel 100%")
    print("  ECG cuts without warning. Agent detects power OFF.")
    print("  waking_hours=True, fuel=100% → generator starts.\n")
    await run_outage(env, hour=8, announced=False, duration_hrs=2.0)
    fuel_report(env, "after Day 2 cut 1")
    outage_header(2, 2, "Plan B", "Unannounced cut 19:00 — evening, fuel reduced")
    print("  Second cut. Fuel reduced but still above 30% threshold.\n")
    await run_outage(env, hour=19, announced=False, duration_hrs=1.5)
    day_cost = env.total_cost_GHS - cost_before
    daily_summary(env, 2, "Tuesday", outages=2,
                  gen_today=env.generator_starts - gen_before, cost_today=day_cost)
    week_summary.append({"day":2,"name":"Tuesday","plan":"B x2",
                         "outages":2,"gen_starts":env.generator_starts-gen_before,
                         "cost":day_cost,"fuel_end":env.fuel_pct})

    # ═══════════════════════════════════════════════════════════════════════
    # DAY 3 — WEDNESDAY
    # ═══════════════════════════════════════════════════════════════════════
    day_header(3, "Wednesday", "Plan C x1 — Announced scheduled cut")
    gen_before  = env.generator_starts
    cost_before = env.total_cost_GHS
    outage_header(1, 3, "Plan C",
                  "Scheduled cut 14:00 — ECG announced, cost calculated upfront")
    print("  ECG announced this cut. Agent reads schedule.")
    print("  Calculates expected fuel cost before cut starts.\n")
    await run_outage(env, hour=14, announced=True, duration_hrs=3.5)
    day_cost = env.total_cost_GHS - cost_before
    daily_summary(env, 3, "Wednesday", outages=1,
                  gen_today=env.generator_starts - gen_before, cost_today=day_cost)
    week_summary.append({"day":3,"name":"Wednesday","plan":"C",
                         "outages":1,"gen_starts":env.generator_starts-gen_before,
                         "cost":day_cost,"fuel_end":env.fuel_pct})

    # ═══════════════════════════════════════════════════════════════════════
    # DAY 4 — THURSDAY
    # ═══════════════════════════════════════════════════════════════════════
    day_header(4, "Thursday", "Plan B → Plan D — fuel goes critical")
    gen_before  = env.generator_starts
    cost_before = env.total_cost_GHS
    outage_header(1, 4, "Plan B", "Unannounced cut 09:00 — fuel still above threshold")
    print("  Fuel reduced from days 2 & 3 but still above 30%.\n")
    await run_outage(env, hour=9, announced=False, duration_hrs=1.5)
    if env.fuel_pct > 19.0:
        env.fuel_pct = 17.0
        log("SIMULATION",
            f"Cumulative week usage → fuel now {env.fuel_pct:.0f}% "
            f"(below Plan D threshold of 20%)", "WARN")
        fuel_report(env, "FUEL CRITICAL — Plan D will activate next cut")
    outage_header(2, 4, "Plan D", "Cut 17:00 — fuel below 20%, Plan D activates")
    print("  fuel_pct=17% < 20% → Plan D activates over XMPP.")
    print("  Generator withheld. Critical alert sent to homeowner.\n")
    await run_outage(env, hour=17, announced=False, duration_hrs=2.0)
    day_cost = env.total_cost_GHS - cost_before
    daily_summary(env, 4, "Thursday", outages=2,
                  gen_today=env.generator_starts - gen_before, cost_today=day_cost)
    week_summary.append({"day":4,"name":"Thursday","plan":"B → D",
                         "outages":2,"gen_starts":env.generator_starts-gen_before,
                         "cost":day_cost,"fuel_end":env.fuel_pct})

    # ═══════════════════════════════════════════════════════════════════════
    # DAY 5 — FRIDAY
    # ═══════════════════════════════════════════════════════════════════════
    day_header(5, "Friday", "Refuel + Plan C repeat — homeowner acts on Day 4 alert")
    gen_before  = env.generator_starts
    cost_before = env.total_cost_GHS
    print("  Homeowner received the XMPP → NotifierAgent alert.")
    print("  Refuelled this morning. Tank topped up by 8L.\n")
    env.refuel(litres=8.0)
    agent.active_plan = "A"
    log("SIMULATION",
        f"Refuelled +8.0L | Tank: {env.fuel_pct:.1f}% ({env.fuel_litres:.2f}L)", "OK")
    fuel_report(env, "after refuel — ready for Day 5")
    outage_header(1, 5, "Plan C (repeat)",
                  "Scheduled cut 14:00 — fresh fuel, calm management")
    await run_outage(env, hour=14, announced=True, duration_hrs=2.5)
    day_cost = env.total_cost_GHS - cost_before
    daily_summary(env, 5, "Friday", outages=1,
                  gen_today=env.generator_starts - gen_before, cost_today=day_cost)
    week_summary.append({"day":5,"name":"Friday","plan":"C (repeat)",
                         "outages":1,"gen_starts":env.generator_starts-gen_before,
                         "cost":day_cost,"fuel_end":env.fuel_pct})

    # ═══════════════════════════════════════════════════════════════════════
    # DAY 6 — SATURDAY
    # ═══════════════════════════════════════════════════════════════════════
    day_header(6, "Saturday", "Plan B night cut — time-of-day logic saves fuel")
    gen_before  = env.generator_starts
    cost_before = env.total_cost_GHS
    outage_header(1, 6, "Plan B (night)",
                  "Unannounced cut 02:00 — night, generator withheld")
    print("  2am. is_waking_hours=False.")
    print("  Agent withholds generator — saves fuel for morning.\n")
    await run_outage(env, hour=2, announced=False, duration_hrs=3.0)
    day_cost = env.total_cost_GHS - cost_before
    daily_summary(env, 6, "Saturday", outages=1,
                  gen_today=env.generator_starts - gen_before, cost_today=day_cost)
    week_summary.append({"day":6,"name":"Saturday","plan":"B (night)",
                         "outages":1,"gen_starts":env.generator_starts-gen_before,
                         "cost":day_cost,"fuel_end":env.fuel_pct})

    # ═══════════════════════════════════════════════════════════════════════
    # DAY 7 — SUNDAY
    # ═══════════════════════════════════════════════════════════════════════
    day_header(7, "Sunday", "Plan B + Plan D repeat — end-of-week depletion")
    gen_before  = env.generator_starts
    cost_before = env.total_cost_GHS
    outage_header(1, 7, "Plan B (repeat)",
                  "Unannounced cut 10:00 — Sunday morning, fuel ok")
    await run_outage(env, hour=10, announced=False, duration_hrs=1.5)
    if env.fuel_pct > 19.0:
        env.fuel_pct = 15.0
        log("SIMULATION",
            f"End-of-week depletion: fuel at {env.fuel_pct:.0f}% — "
            f"Plan D will activate", "WARN")
        fuel_report(env, "end-of-week fuel critical")
    outage_header(2, 7, "Plan D (repeat)",
                  "Cut 20:00 — week's usage exhausted tank, Plan D again")
    print("  Plan D fires consistently — same reasoning as Day 4.")
    print("  Homeowner must refuel before next week.\n")
    await run_outage(env, hour=20, announced=False, duration_hrs=2.0)
    day_cost = env.total_cost_GHS - cost_before
    daily_summary(env, 7, "Sunday", outages=2,
                  gen_today=env.generator_starts - gen_before, cost_today=day_cost)
    week_summary.append({"day":7,"name":"Sunday","plan":"B → D (repeat)",
                         "outages":2,"gen_starts":env.generator_starts-gen_before,
                         "cost":day_cost,"fuel_end":env.fuel_pct})

    # ═══════════════════════════════════════════════════════════════════════
    # WEEKLY SUMMARY
    # ═══════════════════════════════════════════════════════════════════════
    print("\n\n" + "="*64)
    print("  DUMSOR GUARD — 7-DAY WEEKLY SUMMARY")
    print("="*64)
    print(f"  {'Day':<4}  {'Name':<11}  {'Plan':<22}  "
          f"{'Cuts':<5}  {'Gen':<4}  {'₵ Cost':<9}  Fuel end")
    print(f"  {'─'*4}  {'─'*11}  {'─'*22}  "
          f"{'─'*5}  {'─'*4}  {'─'*9}  {'─'*8}")
    for d in week_summary:
        print(f"  {d['day']:<4}  {d['name']:<11}  {d['plan']:<22}  "
              f"{d['outages']:<5}  {d['gen_starts']:<4}  "
              f"₵{d['cost']:<8.2f}  {d['fuel_end']:.1f}%")
    print(f"  {'─'*4}  {'─'*11}  {'─'*22}  "
          f"{'─'*5}  {'─'*4}  {'─'*9}  {'─'*8}")
    print(f"  {'TOTAL':<4}  {'':11}  {'':22}  "
          f"{sum(d['outages'] for d in week_summary):<5}  "
          f"{sum(d['gen_starts'] for d in week_summary):<4}  "
          f"₵{sum(d['cost'] for d in week_summary):.2f}")
    print("="*64)
    print(f"\n  Fuel started    : 10.00L  (100%)")
    print(f"  Fuel consumed   : {env.fuel_consumed_L:.2f}L")
    print(f"  Fuel remaining  : {env.fuel_litres:.2f}L  ({env.fuel_pct:.1f}%)")
    print(f"  Total cost      : ₵{env.total_cost_GHS:.2f}")
    print(f"  Total outages   : {env.outage_count}")
    print(f"  Generator starts: {env.generator_starts}")
    print("="*64)
    print("  PLANS DEMONSTRATED (via SPADE + xmpp.jp):")
    print("  Plan A — Day 1          (baseline monitoring)")
    print("  Plan B — Day 2 x2       (unannounced, waking hours)")
    print("  Plan C — Day 3          (scheduled, cost upfront)")
    print("  Plan D — Day 4 cut 2    (fuel critical, gen withheld)")
    print("  Plan C — Day 5 repeat   (after homeowner refuels)")
    print("  Plan B — Day 6 night    (time-of-day, gen withheld)")
    print("  Plan D — Day 7 repeat   (end-of-week depletion)")
    print("="*64)

    agent.print_summary()
    notifier.print_summary()

    await agent.stop()
    await notifier.stop()
    print("\n  Both SPADE agents stopped. XMPP connections closed.")
    print("  7-day simulation complete.\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        spade.run(run_simulation())
    except KeyboardInterrupt:
        print("\n  Simulation interrupted.")
        sys.exit(0)
    except ConnectionRefusedError:
        print("\n  ✖ Cannot connect to xmpp.jp.")
        print("  Check your internet connection and try again.\n")
        sys.exit(1)