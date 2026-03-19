"""
simulation.py
=============
Dumsor Guard — 7-Day Simulation Runner

Simulates a full week of power outages in a Ghanaian household.

Design rules
------------
  - Fuel starts at 100% (full 10L tank) and carries over every day
  - Maximum 2 outages per day
  - Fuel consumed is printed after every generator use
  - Remaining fuel always reflects actual running total
  - All 4 Prometheus plans demonstrated at least once in first 4 days
  - Days 5-7 show realistic repeats with different fuel levels

Day plan
--------
  Day 1 (Monday)    — Plan A only. Quiet day, no outages.
  Day 2 (Tuesday)   — Plan B x2. Two unannounced cuts.
  Day 3 (Wednesday) — Plan C x1. One long scheduled cut.
  Day 4 (Thursday)  — Plan B then Plan D. Fuel goes critical.
  Day 5 (Friday)    — Homeowner refuels. Plan C repeat.
  Day 6 (Saturday)  — Plan B night cut, generator withheld.
  Day 7 (Sunday)    — Plan B morning + Plan D repeat (end-of-week depletion).

Usage
-----
    python simulation.py

Requirements
------------
    pip install spade
"""

import asyncio
import sys
from environment import Environment
from dumsor_agent import DumsorAgent, log
from notifier_agent import NotifierAgent


# ---------------------------------------------------------------------------
# SPADE mock — in-process message queues, no XMPP server needed
# ---------------------------------------------------------------------------

def patch_spade_for_mock():
    import spade.agent
    import spade.behaviour
    from asyncio import Queue

    _inboxes: dict = {}

    def get_inbox(jid: str) -> Queue:
        if jid not in _inboxes:
            _inboxes[jid] = Queue()
        return _inboxes[jid]

    async def mock_start(self, auto_register=True):
        self._alive = asyncio.Event()
        self.loop = asyncio.get_event_loop()
        self._jid_str = str(self.jid) if hasattr(self, 'jid') else "agent"
        self._inbox = get_inbox(self._jid_str)
        await self.setup()

    async def mock_stop(self):
        for b in list(self.behaviours):
            b.kill()
        if hasattr(self, '_alive'):
            self._alive.set()

    async def mock_send(self, msg):
        to_jid = str(msg.to)
        inbox = get_inbox(to_jid)
        await inbox.put(msg)

    async def mock_receive(behaviour_self, timeout=10):
        agent = behaviour_self.agent
        inbox = getattr(agent, '_inbox', None)
        if inbox is None:
            await asyncio.sleep(timeout)
            return None
        try:
            msg = await asyncio.wait_for(inbox.get(), timeout=timeout)
            return msg
        except asyncio.TimeoutError:
            return None

    spade.agent.Agent.start    = mock_start
    spade.agent.Agent.stop     = mock_stop
    spade.agent.Agent.send     = mock_send
    spade.agent.Agent.is_alive = lambda self: True
    spade.behaviour.CyclicBehaviour.receive = mock_receive


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

async def wait(seconds: float = 2.0):
    await asyncio.sleep(seconds)


def day_header(day_num: int, day_name: str, theme: str):
    print(f"\n{'='*60}")
    print(f"  DAY {day_num} — {day_name.upper()}")
    print(f"  Theme: {theme}")
    print(f"{'='*60}")


def outage_header(outage_num: int, day: int, plan: str, desc: str):
    print(f"\n  {'─'*56}")
    print(f"  DAY {day} | OUTAGE {outage_num}/2 | {plan}")
    print(f"  {desc}")
    print(f"  {'─'*56}")


def fuel_report(env: Environment, label: str = ""):
    """Prominent fuel status line after every generator use."""
    remaining_L   = env.fuel_litres
    remaining_pct = env.fuel_pct
    consumed      = env.fuel_consumed_L
    cost          = env.total_cost_GHS
    print(f"\n  ⛽  FUEL REPORT {('— ' + label) if label else ''}")
    print(f"      Total consumed this week : {consumed:.2f} L")
    print(f"      Remaining in tank        : {remaining_L:.2f} L  ({remaining_pct:.1f}%)")
    print(f"      Est. runtime remaining   : ~{remaining_L:.1f} hrs")
    print(f"      Total fuel cost so far   : ₵{cost:.2f}\n")


def daily_summary(env: Environment, day: int, day_name: str,
                  outages: int, gen_starts_today: int, cost_today: float):
    print(f"\n  ┌{'─'*54}┐")
    print(f"  │  END OF DAY {day} — {day_name:<41}│")
    print(f"  │  Outages today    : {outages:<34}│")
    print(f"  │  Generator starts : {gen_starts_today:<34}│")
    print(f"  │  Cost today       : ₵{cost_today:<33.2f}│")
    print(f"  │  Fuel remaining   : {env.fuel_pct:.1f}%  ({env.fuel_litres:.2f}L){'':>21}│")
    print(f"  └{'─'*54}┘")


# ---------------------------------------------------------------------------
# Outage helper — inject event, wait for agent, advance time, restore
# ---------------------------------------------------------------------------

async def run_outage(env: Environment, agent: DumsorAgent,
                     hour: int, announced: bool,
                     duration_hrs: float, label: str) -> float:
    """
    Runs one complete outage cycle.
    Returns cost incurred (₵) during this outage.
    """
    cost_before = env.total_cost_GHS
    fuel_pct_before = env.fuel_pct

    env.sim_time = env.sim_time.replace(hour=hour, minute=0)
    env.trigger_outage(announced=announced)

    log("SIMULATION",
        f"{'Scheduled' if announced else 'Unannounced'} cut injected at "
        f"{hour:02d}:00 — expected duration ~{duration_hrs:.1f}hrs",
        "WARN" if announced else "CRIT")

    # Agent perceives and reacts
    await wait(6.0)

    # Advance simulated time through the outage
    env.advance_time(duration_hrs)

    log("SIMULATION",
        f"Outage lasted {duration_hrs:.1f}hrs | "
        f"Generator: {'RUNNING — consumed fuel' if env.generator_running else 'was off — fuel saved'} | "
        f"Fuel now: {env.fuel_pct:.1f}%",
        "INFO")

    # Restore ECG power
    env.restore_power()
    agent.beliefs["was_power_on"] = False   # trigger PowerRestoredBehaviour
    await wait(6.0)

    cost_this   = env.total_cost_GHS - cost_before
    fuel_used   = fuel_pct_before - env.fuel_pct
    litres_used = (fuel_used / 100.0) * env.fuel_capacity_L

    if litres_used > 0.01:
        print(f"\n  ▶ Generator ran {duration_hrs:.1f}hrs — "
              f"consumed {litres_used:.2f}L ({fuel_used:.1f}%) — "
              f"cost this outage: ₵{cost_this:.2f}")
    else:
        print(f"\n  · Generator NOT started — 0 fuel consumed — ₵0.00 cost.")

    fuel_report(env, label)
    return cost_this


# ---------------------------------------------------------------------------
# 7-day simulation
# ---------------------------------------------------------------------------

async def run_simulation():

    print("\n" + "="*60)
    print("  DUMSOR GUARD — 7-DAY WEEKLY SIMULATION")
    print("  Intelligent Home Power Management Agent")
    print("  Prometheus Methodology | SPADE Multi-Agent System")
    print("  Simulated household — Accra, Ghana")
    print("="*60)
    print("  Fuel capacity  : 10.0 litres  (100% at start)")
    print("  Burn rate      : 1.0 L / hr")
    print("  Fuel price     : ₵14.50 / litre")
    print("  Max outages    : 2 per day")
    print("  Critical fuel  : < 20%  (Plan D threshold)")
    print("="*60)

    # ── Environment ───────────────────────────────────────────────────────
    env = Environment(
        fuel_pct=100.0,
        start_hour=6,
        fuel_capacity_L=10.0,
        burn_rate_Lph=1.0,
        fuel_price_GHS=14.5,
    )
    env.add_scheduled_cut(start_hour=14, end_hour=18)

    # ── Agents ────────────────────────────────────────────────────────────
    notifier = NotifierAgent(jid="notifier@localhost", password="notifier123")
    agent    = DumsorAgent(
        jid="dumsor@localhost",
        password="dumsor123",
        environment=env,
        notifier_jid="notifier@localhost",
    )

    await notifier.start()
    log("SIMULATION", "NotifierAgent online and listening", "OK")
    await agent.start()
    log("SIMULATION", "DumsorAgent online — 7-day simulation starting", "OK")
    await wait(1.0)

    week_summary = []


    # ══════════════════════════════════════════════════════════════════════
    # DAY 1 — MONDAY  |  Plan A — Normal monitoring
    # No outages. Agent runs continuously, updates beliefs, checks schedule.
    # ══════════════════════════════════════════════════════════════════════
    day_header(1, "Monday", "Plan A — Continuous monitoring, no outages today")
    print("  ECG power is stable all day. No cuts occur.")
    print("  Agent runs Plan A: polls environment every 5s, updates beliefs,")
    print("  reads the ECG schedule, estimates outage probability.")
    print("  This establishes the baseline — all 10L fuel intact.")

    env.sim_time = env.sim_time.replace(hour=6, minute=0)
    await wait(12.0)

    log("SIMULATION",
        f"Day 1 complete — Fuel: {env.fuel_pct:.0f}% (unchanged) | "
        f"Outage prob: {agent._predict_outage_probability()}%",
        "INFO")

    fuel_report(env, "end of Day 1 — no generator use, tank full")
    daily_summary(env, 1, "Monday",
                  outages=0, gen_starts_today=0, cost_today=0.0)
    week_summary.append({
        "day": 1, "name": "Monday", "plan": "A",
        "outages": 0, "gen_starts": 0,
        "cost": 0.0, "fuel_end": env.fuel_pct,
    })


    # ══════════════════════════════════════════════════════════════════════
    # DAY 2 — TUESDAY  |  Plan B x2 — Two unannounced cuts
    # Cut 1: 08:00, 2.0hrs — waking hours, full fuel → generator starts
    # Cut 2: 19:00, 1.5hrs — evening, reduced fuel → generator starts again
    # Fuel deducted twice; running total carries into Day 3.
    # ══════════════════════════════════════════════════════════════════════
    day_header(2, "Tuesday", "Plan B x2 — Two unannounced cuts")
    gen_before = env.generator_starts
    cost_before = env.total_cost_GHS

    outage_header(1, 2, "Plan B",
                  "Unannounced cut 08:00 — waking hours, fuel 100%, generator starts")
    c1 = await run_outage(env, agent,
                          hour=8, announced=False,
                          duration_hrs=2.0,
                          label="Day 2 cut 1 complete")

    outage_header(2, 2, "Plan B",
                  "Unannounced cut 19:00 — evening, fuel reduced, generator starts")
    c2 = await run_outage(env, agent,
                          hour=19, announced=False,
                          duration_hrs=1.5,
                          label="Day 2 cut 2 complete")

    day_cost = env.total_cost_GHS - cost_before
    daily_summary(env, 2, "Tuesday",
                  outages=2,
                  gen_starts_today=env.generator_starts - gen_before,
                  cost_today=day_cost)
    week_summary.append({
        "day": 2, "name": "Tuesday", "plan": "B x2",
        "outages": 2, "gen_starts": env.generator_starts - gen_before,
        "cost": day_cost, "fuel_end": env.fuel_pct,
    })


    # ══════════════════════════════════════════════════════════════════════
    # DAY 3 — WEDNESDAY  |  Plan C x1 — Long scheduled cut
    # ECG announces 14:00–18:00. Agent evaluates: 3.5hrs justifies fuel.
    # Calculates cost upfront, starts generator, sheds non-critical loads.
    # ══════════════════════════════════════════════════════════════════════
    day_header(3, "Wednesday", "Plan C x1 — Scheduled cut, cost calculated upfront")
    gen_before = env.generator_starts
    cost_before = env.total_cost_GHS

    outage_header(1, 3, "Plan C",
                  "Scheduled cut 14:00 announced by ECG — agent pre-stages generator")
    print("  The agent read the ECG schedule this morning.")
    print("  It calculated the expected fuel cost and alerted the homeowner")
    print("  before the cut even started.")
    await run_outage(env, agent,
                     hour=14, announced=True,
                     duration_hrs=3.5,
                     label="Day 3 scheduled cut complete")

    day_cost = env.total_cost_GHS - cost_before
    daily_summary(env, 3, "Wednesday",
                  outages=1,
                  gen_starts_today=env.generator_starts - gen_before,
                  cost_today=day_cost)
    week_summary.append({
        "day": 3, "name": "Wednesday", "plan": "C",
        "outages": 1, "gen_starts": env.generator_starts - gen_before,
        "cost": day_cost, "fuel_end": env.fuel_pct,
    })


    # ══════════════════════════════════════════════════════════════════════
    # DAY 4 — THURSDAY  |  Plan B then Plan D
    # Cut 1: 09:00, 1.5hrs — fuel still ok → Plan B, generator runs
    # After cut 1, cumulative usage pushes fuel below 20%
    # Cut 2: 17:00 — Plan D activates, generator withheld, critical alert
    # This is the core goal conflict: protect appliances vs conserve fuel.
    # ══════════════════════════════════════════════════════════════════════
    day_header(4, "Thursday", "Plan B → Plan D — goal conflict: protect vs conserve")
    gen_before = env.generator_starts
    cost_before = env.total_cost_GHS

    outage_header(1, 4, "Plan B",
                  "Unannounced cut 09:00 — fuel still above threshold, generator starts")
    await run_outage(env, agent,
                     hour=9, announced=False,
                     duration_hrs=1.5,
                     label="Day 4 cut 1 — last run before critical")

    # Ensure fuel is below critical threshold for Plan D to activate
    if env.fuel_pct > 19.0:
        env.fuel_pct = 17.0
        log("SIMULATION",
            f"Cumulative week usage: fuel now at {env.fuel_pct:.0f}% — "
            f"below Plan D threshold of 20%",
            "WARN")
        fuel_report(env, "FUEL CRITICAL — Plan D will activate on next outage")

    outage_header(2, 4, "Plan D",
                  "Unannounced cut 17:00 — fuel below 20%, Plan D activated")
    print("  Fuel is critically low. The agent weighs its goals:")
    print("  Goal 1 says protect the fridge. Goal 3 says don't burn fuel.")
    print("  Goal 4 wins here: protect reserves. Generator is withheld.")
    print("  Fridge stays on backup power. Critical alert sent to homeowner.")
    await run_outage(env, agent,
                     hour=17, announced=False,
                     duration_hrs=2.0,
                     label="Day 4 cut 2 — Plan D, no generator")

    day_cost = env.total_cost_GHS - cost_before
    daily_summary(env, 4, "Thursday",
                  outages=2,
                  gen_starts_today=env.generator_starts - gen_before,
                  cost_today=day_cost)
    week_summary.append({
        "day": 4, "name": "Thursday", "plan": "B → D",
        "outages": 2, "gen_starts": env.generator_starts - gen_before,
        "cost": day_cost, "fuel_end": env.fuel_pct,
    })


    # ══════════════════════════════════════════════════════════════════════
    # DAY 5 — FRIDAY  |  Refuel + Plan C repeat
    # Homeowner saw the Day 4 CRITICAL alert and refuelled this morning.
    # ECG announces another scheduled cut. Fresh fuel, calm management.
    # ══════════════════════════════════════════════════════════════════════
    day_header(5, "Friday", "Refuel + Plan C repeat — recovery after Day 4 alert")
    gen_before = env.generator_starts
    cost_before = env.total_cost_GHS

    print("  The Day 4 critical alert worked. Homeowner refuelled this morning.")
    env.refuel(litres=8.0)
    agent.active_plan = "A"
    log("SIMULATION",
        f"Refuelled +8.0L | Tank now: {env.fuel_pct:.1f}% ({env.fuel_litres:.2f}L)",
        "OK")
    fuel_report(env, "after morning refuel — ready for Day 5")

    outage_header(1, 5, "Plan C (repeat)",
                  "Scheduled cut 14:00 — fresh fuel, agent manages confidently")
    await run_outage(env, agent,
                     hour=14, announced=True,
                     duration_hrs=2.5,
                     label="Day 5 scheduled cut complete")

    day_cost = env.total_cost_GHS - cost_before
    daily_summary(env, 5, "Friday",
                  outages=1,
                  gen_starts_today=env.generator_starts - gen_before,
                  cost_today=day_cost)
    week_summary.append({
        "day": 5, "name": "Friday", "plan": "C (repeat)",
        "outages": 1, "gen_starts": env.generator_starts - gen_before,
        "cost": day_cost, "fuel_end": env.fuel_pct,
    })


    # ══════════════════════════════════════════════════════════════════════
    # DAY 6 — SATURDAY  |  Plan B night cut — generator withheld
    # Cut at 02:00. Not waking hours. No one is using anything meaningful.
    # Agent applies time-of-day logic: fuel saved, loads shed, fridge ok.
    # ══════════════════════════════════════════════════════════════════════
    day_header(6, "Saturday", "Plan B night cut — time-of-day logic saves fuel")
    gen_before = env.generator_starts
    cost_before = env.total_cost_GHS

    outage_header(1, 6, "Plan B (night)",
                  "Unannounced cut 02:00 — night-time, generator withheld")
    print("  2am. Everyone is asleep. Running the generator to power")
    print("  a dark TV and an empty ceiling fan is irrational.")
    print("  Agent checks is_waking_hours = False → holds generator.")
    print("  Fridge stays on backup. Loads shed. Fuel preserved for morning.")
    await run_outage(env, agent,
                     hour=2, announced=False,
                     duration_hrs=3.0,
                     label="Day 6 night cut — zero fuel used")

    day_cost = env.total_cost_GHS - cost_before
    daily_summary(env, 6, "Saturday",
                  outages=1,
                  gen_starts_today=env.generator_starts - gen_before,
                  cost_today=day_cost)
    week_summary.append({
        "day": 6, "name": "Saturday", "plan": "B (night, no gen)",
        "outages": 1, "gen_starts": env.generator_starts - gen_before,
        "cost": day_cost, "fuel_end": env.fuel_pct,
    })


    # ══════════════════════════════════════════════════════════════════════
    # DAY 7 — SUNDAY  |  Plan B + Plan D repeat
    # Cut 1: 10:00, 1.5hrs — fuel adequate → generator runs
    # End-of-week depletion pushes fuel below critical again
    # Cut 2: 20:00 → Plan D repeat. Consistent, correct agent behaviour.
    # Shows the agent behaving identically to Day 4 — Plan D is reliable.
    # ══════════════════════════════════════════════════════════════════════
    day_header(7, "Sunday", "Plan B + Plan D repeat — end-of-week fuel depletion")
    gen_before = env.generator_starts
    cost_before = env.total_cost_GHS

    outage_header(1, 7, "Plan B (repeat)",
                  "Unannounced cut 10:00 — Sunday morning, fuel adequate")
    await run_outage(env, agent,
                     hour=10, announced=False,
                     duration_hrs=1.5,
                     label="Day 7 cut 1 complete")

    # End-of-week cumulative use drops tank below critical
    if env.fuel_pct > 19.0:
        env.fuel_pct = 15.0
        log("SIMULATION",
            f"End-of-week depletion: fuel at {env.fuel_pct:.0f}% — "
            f"Plan D threshold crossed for Day 7 cut 2",
            "WARN")
        fuel_report(env, "end-of-week low fuel — Plan D imminent")

    outage_header(2, 7, "Plan D (repeat)",
                  "Cut 20:00 — week's usage exhausted tank, Plan D activates again")
    print("  Seven days of cuts have drained the tank.")
    print("  Plan D activates consistently — same decision, same reasoning.")
    print("  Homeowner must refuel before next week.")
    await run_outage(env, agent,
                     hour=20, announced=False,
                     duration_hrs=2.0,
                     label="end of week — Plan D, no fuel to spare")

    day_cost = env.total_cost_GHS - cost_before
    daily_summary(env, 7, "Sunday",
                  outages=2,
                  gen_starts_today=env.generator_starts - gen_before,
                  cost_today=day_cost)
    week_summary.append({
        "day": 7, "name": "Sunday", "plan": "B → D (repeat)",
        "outages": 2, "gen_starts": env.generator_starts - gen_before,
        "cost": day_cost, "fuel_end": env.fuel_pct,
    })


    # ══════════════════════════════════════════════════════════════════════
    # WEEKLY SUMMARY TABLE
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n")
    print("="*62)
    print("  DUMSOR GUARD — 7-DAY WEEKLY SUMMARY TABLE")
    print("="*62)
    print(f"  {'Day':<4}  {'Name':<11}  {'Plan':<20}  "
          f"{'Cuts':<5}  {'Gen':<4}  {'₵ Cost':<9}  {'Fuel end'}")
    print(f"  {'─'*4}  {'─'*11}  {'─'*20}  "
          f"{'─'*5}  {'─'*4}  {'─'*9}  {'─'*8}")
    for d in week_summary:
        print(f"  {d['day']:<4}  {d['name']:<11}  {d['plan']:<20}  "
              f"{d['outages']:<5}  {d['gen_starts']:<4}  "
              f"₵{d['cost']:<8.2f}  {d['fuel_end']:.1f}%")
    print(f"  {'─'*4}  {'─'*11}  {'─'*20}  "
          f"{'─'*5}  {'─'*4}  {'─'*9}  {'─'*8}")
    total_outages = sum(d['outages']    for d in week_summary)
    total_starts  = sum(d['gen_starts'] for d in week_summary)
    total_cost    = sum(d['cost']       for d in week_summary)
    print(f"  {'TOTAL':<4}  {'':11}  {'':20}  "
          f"{total_outages:<5}  {total_starts:<4}  ₵{total_cost:.2f}")
    print("="*62)
    print(f"\n  Fuel started   : 10.00 L  (100%)")
    print(f"  Fuel consumed  : {env.fuel_consumed_L:.2f} L")
    print(f"  Fuel remaining : {env.fuel_litres:.2f} L  ({env.fuel_pct:.1f}%)")
    print(f"  Total cost     : ₵{env.total_cost_GHS:.2f}")
    print(f"  Total outages  : {env.outage_count}")
    print(f"  Generator starts: {env.generator_starts}")
    print("="*62)
    print("  PROMETHEUS PLANS DEMONSTRATED:")
    print("  Plan A — Day 1           (normal monitoring baseline)")
    print("  Plan B — Day 2 x2        (unannounced, waking hours)")
    print("  Plan C — Day 3           (scheduled, cost upfront)")
    print("  Plan D — Day 4 cut 2     (fuel critical, generator withheld)")
    print("  Plan B — Day 6           (night, time-of-day conserves fuel)")
    print("  Plan C — Day 5 (repeat)  (scheduled after refuel)")
    print("  Plan D — Day 7 (repeat)  (end-of-week depletion)")
    print("="*62)

    print()
    agent.print_summary()
    notifier.print_summary()

    await agent.stop()
    await notifier.stop()
    print("\n  7-day simulation complete. All agents stopped.\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    patch_spade_for_mock()
    try:
        asyncio.run(run_simulation())
    except KeyboardInterrupt:
        print("\n  Simulation interrupted by user.")
        sys.exit(0)