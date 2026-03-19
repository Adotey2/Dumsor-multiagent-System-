"""
dumsor_agent.py
===============
Dumsor Guard — SPADE Agent Implementation

This file implements the intelligent agent using SPADE's behaviour system.
Each Prometheus plan maps directly to a SPADE behaviour class:

  Plan A → NormalMonitoringBehaviour    (PeriodicBehaviour)
  Plan B → UnnouncedCutBehaviour        (OneShotBehaviour)
  Plan C → ScheduledCutBehaviour        (OneShotBehaviour)
  Plan D → FuelConservationBehaviour    (OneShotBehaviour)

The agent's belief base is stored in self.beliefs (a dict on the agent).
Percepts arrive from the shared Environment object each cycle.
Actions are method calls back on the Environment.

Prometheus Mapping
------------------
  Goals      → defined as constants in DumsorAgent
  Percepts   → read via env.percepts each cycle
  Actions    → env.start_generator(), env.shed_load(), etc.
  Plans      → SPADE Behaviour subclasses
  Events     → detected in NormalMonitoringBehaviour, trigger plan switches
  Beliefs    → self.beliefs dict, updated every perception cycle
"""

import asyncio
import json
from datetime import datetime
from typing import Optional

import spade
from spade.agent import Agent
from spade.behaviour import PeriodicBehaviour, OneShotBehaviour
from spade.message import Message

from environment import Environment


# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------

def log(agent_name: str, msg: str, level: str = "INFO"):
    """
    Structured agent log output.
    level: INFO | WARN | CRIT | OK | ACTION
    """
    icons = {"INFO": "·", "WARN": "⚠", "CRIT": "✖", "OK": "✓", "ACTION": "▶"}
    icon = icons.get(level, "·")
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] [{agent_name}] {icon} [{level}] {msg}")


# ===========================================================================
# PLAN A — Normal Monitoring Behaviour
# Runs every 5 seconds (simulating a 5-minute polling cycle).
# Checks percepts, updates beliefs, detects events, switches plans.
# ===========================================================================

class NormalMonitoringBehaviour(PeriodicBehaviour):
    """
    Prometheus Plan A — Continuous background monitoring.

    The agent's main perception-decision loop. Runs periodically and:
      1. Reads all percepts from the environment
      2. Updates the agent's belief base
      3. Detects events (power cut, low fuel, scheduled cut approaching)
      4. Triggers the appropriate reactive behaviour
    """

    async def run(self):
        agent = self.agent
        env: Environment = agent.environment
        percepts = env.percepts

        # ── 1. Update belief base ──────────────────────────────────────────
        agent.beliefs.update({
            "power_on":          percepts["power_on"],
            "generator_running": percepts["generator_running"],
            "fuel_pct":          percepts["fuel_pct"],
            "fuel_litres":       percepts["fuel_litres"],
            "fuel_runtime_hrs":  percepts["fuel_runtime_hrs"],
            "hour":              percepts["hour"],
            "is_waking_hours":   percepts["is_waking_hours"],
            "outage_count":      percepts["outage_count"],
            "outage_history":    percepts["outage_history"],
            "last_updated":      datetime.now().isoformat(),
        })

        # ── 2. Detect and handle events ────────────────────────────────────

        # EVENT: Fuel critically low → Plan D
        if percepts["fuel_pct"] < agent.FUEL_CRITICAL_PCT and agent.active_plan != "D":
            log(agent.name, f"Fuel at {percepts['fuel_pct']:.1f}% — activating Plan D (fuel conservation)", "CRIT")
            agent.active_plan = "D"
            b = FuelConservationBehaviour()
            agent.add_behaviour(b)

        # EVENT: Power just cut off (was on, now off) → Plan B or C
        elif not percepts["power_on"] and agent.beliefs.get("was_power_on", True):
            if percepts["scheduled_cut"]:
                log(agent.name, "Scheduled ECG outage detected → Plan C", "WARN")
                agent.active_plan = "C"
                b = ScheduledCutBehaviour()
                agent.add_behaviour(b)
            else:
                log(agent.name, "UNANNOUNCED power cut detected → Plan B", "CRIT")
                agent.active_plan = "B"
                b = UnannouncedCutBehaviour()
                agent.add_behaviour(b)

        # EVENT: Power restored while we were managing an outage
        elif percepts["power_on"] and not agent.beliefs.get("was_power_on", True):
            log(agent.name, "ECG power restored — returning to Plan A", "OK")
            agent.active_plan = "A"
            b = PowerRestoredBehaviour()
            agent.add_behaviour(b)

        # NORMAL: Check scheduled cuts approaching
        else:
            upcoming = self._check_upcoming_scheduled_cut(percepts)
            if upcoming and agent.active_plan == "A":
                log(agent.name, f"Scheduled cut approaching at hour {upcoming} — alerting homeowner", "WARN")
                agent.send_alert(
                    f"⚠ Scheduled ECG cut expected around {upcoming:02d}:00. "
                    f"Fuel: {percepts['fuel_pct']:.0f}%. Prepare now.",
                    "WARN"
                )

            if agent.active_plan == "A":
                prob = agent._predict_outage_probability()
                log(agent.name,
                    f"Monitoring | Power: ON | Fuel: {percepts['fuel_pct']:.0f}% "
                    f"| Outage prob (1hr): {prob}% | Plan: A",
                    "INFO")

        # Track previous power state for edge detection
        agent.beliefs["was_power_on"] = percepts["power_on"]

    def _check_upcoming_scheduled_cut(self, percepts: dict) -> Optional[int]:
        """Return the start hour of an upcoming scheduled cut, or None."""
        current_hour = percepts["hour"]
        for (start, end) in percepts["ecg_schedule"]:
            if 0 <= (start - current_hour) <= 2:
                return start
        return None


# ===========================================================================
# PLAN B — Unannounced Cut Behaviour
# ===========================================================================

class UnannouncedCutBehaviour(OneShotBehaviour):
    """
    Prometheus Plan B — React to an unannounced ECG outage.

    Decision logic:
      - If waking hours AND fuel > LOW threshold → start generator, shed non-critical
      - If night OR fuel critically low → do NOT start generator, send alert only
      - Always protect critical appliances
    """

    async def run(self):
        agent = self.agent
        env: Environment = agent.environment
        percepts = env.percepts

        log(agent.name, "Executing Plan B — Unannounced cut response", "ACTION")

        fuel_ok = percepts["fuel_pct"] >= agent.FUEL_LOW_PCT
        waking = percepts["is_waking_hours"]

        if fuel_ok and waking:
            # Decision: start generator, protect everything possible
            started = env.start_generator()
            if started:
                log(agent.name, "Generator STARTED — fuel sufficient, waking hours", "ACTION")
                agent.stats["generator_starts"] += 1

                # Shed non-critical loads to conserve fuel
                shed = env.shed_non_critical()
                agent.stats["loads_shed"] += len(shed)
                log(agent.name, f"Load shedding — turned off: {', '.join(shed)}", "ACTION")

                # Predict how long this will last
                pred = agent._predict_outage_duration()
                fuel_needed = pred * env.burn_rate_Lph
                agent.send_alert(
                    f"✖ Unannounced power cut! Generator ON. "
                    f"Non-critical loads shed. Est. duration: {pred:.1f}hrs "
                    f"(~{fuel_needed:.1f}L fuel needed). Fridge & router protected.",
                    "CRIT"
                )
            else:
                log(agent.name, "Generator failed to start (no fuel or already running)", "CRIT")
                agent.send_alert("✖ Power cut! Generator could not start — check fuel!", "CRIT")

        elif not fuel_ok:
            # Decision: fuel too low — conserve, critical only
            log(agent.name, f"Fuel low ({percepts['fuel_pct']:.0f}%) — NOT starting generator", "WARN")
            shed = env.shed_non_critical()
            agent.stats["loads_shed"] += len(shed)
            agent.send_alert(
                f"✖ Power cut. Fuel too low to run generator safely "
                f"({percepts['fuel_pct']:.0f}%). Non-critical loads shed. REFUEL URGENTLY.",
                "CRIT"
            )

        else:
            # Night time — not worth burning fuel
            log(agent.name, "Night-time cut — conserving fuel, not starting generator", "WARN")
            shed = env.shed_non_critical()
            agent.stats["loads_shed"] += len(shed)
            agent.send_alert(
                "✖ Power cut (night). Generator held to save fuel. "
                "Fridge on backup. All non-critical loads off.",
                "WARN"
            )


# ===========================================================================
# PLAN C — Scheduled Cut Behaviour
# ===========================================================================

class ScheduledCutBehaviour(OneShotBehaviour):
    """
    Prometheus Plan C — Manage an announced ECG outage.

    The agent already knows this cut is coming (from the ECG schedule),
    so it can be more deliberate. It only starts the generator if the
    predicted duration justifies the fuel cost (> 45 minutes).
    """

    MINIMUM_DURATION_TO_START_HRS = 0.75  # 45 minutes

    async def run(self):
        agent = self.agent
        env: Environment = agent.environment
        percepts = env.percepts

        log(agent.name, "Executing Plan C — Scheduled cut management", "ACTION")

        pred_duration = agent._predict_outage_duration()
        fuel_pct = percepts["fuel_pct"]

        log(agent.name, f"Predicted cut duration: {pred_duration:.1f}hrs | Fuel: {fuel_pct:.0f}%", "INFO")

        if pred_duration < self.MINIMUM_DURATION_TO_START_HRS:
            # Not worth starting for a short cut
            log(agent.name, f"Predicted duration {pred_duration:.1f}hrs < 45min — not starting generator", "INFO")
            agent.send_alert(
                f"⚠ Scheduled cut active. Expected duration ~{pred_duration*60:.0f} min — "
                f"too short to justify generator. Conserving fuel.",
                "INFO"
            )
        elif fuel_pct < agent.FUEL_CRITICAL_PCT:
            log(agent.name, "Fuel critical — cannot run generator for full cut", "CRIT")
            shed = env.shed_non_critical()
            agent.stats["loads_shed"] += len(shed)
            agent.send_alert(
                f"⚠ Scheduled cut. Fuel critically low ({fuel_pct:.0f}%) — "
                f"generator conserved. REFUEL IMMEDIATELY.",
                "CRIT"
            )
        else:
            started = env.start_generator()
            if started:
                agent.stats["generator_starts"] += 1
                shed = env.shed_non_critical()
                agent.stats["loads_shed"] += len(shed)
                log(agent.name, "Generator STARTED for scheduled cut", "ACTION")
                fuel_needed = pred_duration * env.burn_rate_Lph
                cost = fuel_needed * env.fuel_price_GHS
                log(agent.name,
                    f"Estimated fuel cost for this cut: ₵{cost:.2f} ({fuel_needed:.1f}L)",
                    "INFO")
                agent.send_alert(
                    f"⚠ Scheduled cut in progress. Generator ON. "
                    f"Est. duration: {pred_duration:.1f}hrs | "
                    f"Est. cost: ₵{cost:.2f} | Loads shed to save fuel.",
                    "WARN"
                )


# ===========================================================================
# PLAN D — Fuel Conservation Behaviour
# ===========================================================================

class FuelConservationBehaviour(OneShotBehaviour):
    """
    Prometheus Plan D — Fuel conservation mode.

    Triggered when fuel drops below the critical threshold.
    The agent stops all non-essential generator use, alerts the
    homeowner urgently, and calculates remaining critical runtime.
    """

    async def run(self):
        agent = self.agent
        env: Environment = agent.environment
        percepts = env.percepts

        log(agent.name, "Executing Plan D — Fuel conservation mode ACTIVATED", "CRIT")

        # Stop generator if running on non-critical loads
        if percepts["generator_running"]:
            # Shed everything non-critical
            shed = env.shed_non_critical()
            agent.stats["loads_shed"] += len(shed)
            log(agent.name, f"Emergency shedding: {', '.join(shed)}", "ACTION")

        fuel_L = percepts["fuel_litres"]
        # Calculate how long critical-only loads can run
        critical_watts = sum(
            a.watt for a in env.appliances if a.critical
        )
        # Generator efficiency factor (critical loads only = less burn)
        adj_burn_rate = env.burn_rate_Lph * 0.6  # lighter load = ~60% burn rate
        critical_runtime_hrs = fuel_L / adj_burn_rate if adj_burn_rate > 0 else 0

        log(agent.name,
            f"Fuel conservation mode: {fuel_L:.2f}L remaining | "
            f"Critical-only runtime: ~{critical_runtime_hrs:.1f}hrs",
            "CRIT")

        agent.send_alert(
            f"⛽ FUEL CRITICAL: {percepts['fuel_pct']:.0f}% remaining "
            f"({fuel_L:.1f}L). Est. critical runtime: {critical_runtime_hrs:.1f}hrs. "
            f"ALL non-critical loads shed. REFUEL IMMEDIATELY.",
            "CRIT"
        )

        agent.stats["fuel_warnings"] += 1


# ===========================================================================
# Power Restored Behaviour (clean-up plan)
# ===========================================================================

class PowerRestoredBehaviour(OneShotBehaviour):
    """
    Called when ECG power comes back on.
    Stops the generator, restores all shed loads, logs the completed outage.
    """

    async def run(self):
        agent = self.agent
        env: Environment = agent.environment

        log(agent.name, "ECG power restored — executing clean-up", "OK")

        if env.generator_running:
            env.stop_generator()
            log(agent.name, "Generator STOPPED — ECG power restored", "ACTION")

        env.restore_all_loads()
        log(agent.name, "All shed loads RESTORED", "ACTION")

        # Log outage stats
        history = env.percepts["outage_history"]
        if history:
            last = history[-1]
            log(agent.name,
                f"Outage closed | Duration: {last['duration_hrs']:.2f}hrs | "
                f"Announced: {last['announced']} | "
                f"Fuel consumed total: {env.fuel_consumed_L:.2f}L | "
                f"Total cost: ₵{env.total_cost_GHS:.2f}",
                "OK")

        agent.send_alert(
            f"✓ ECG power restored. Generator off. All loads back online. "
            f"Fuel used this session: {env.fuel_consumed_L:.2f}L "
            f"(₵{env.total_cost_GHS:.2f}).",
            "OK"
        )


# ===========================================================================
# The Dumsor Guard Agent
# ===========================================================================

class DumsorAgent(Agent):
    """
    Dumsor Guard — Intelligent Home Power Management Agent.

    Prometheus role  : Primary decision-maker for household power management
    Goals            : See GOAL_* constants below
    Plans            : Plan A (monitoring), B (unannounced cut),
                       C (scheduled cut), D (fuel conservation)
    Percepts         : Read from shared Environment object each cycle
    Actions          : start/stop generator, shed/restore loads, send alerts
    Beliefs          : Updated every perception cycle from percepts

    Parameters
    ----------
    jid         : XMPP Jabber ID (e.g. agent@localhost)
    password    : XMPP password
    environment : Shared Environment instance
    """

    # ── Agent Goals (Prometheus goal hierarchy) ────────────────────────────
    GOAL_PROTECT_CRITICAL   = "protect_critical_appliances"   # priority 1
    GOAL_WARN_USER          = "warn_user_before_disruption"   # priority 2
    GOAL_MINIMISE_FUEL      = "minimise_fuel_consumption"     # priority 3
    GOAL_PROTECT_RESERVES   = "protect_fuel_reserves"         # priority 4
    GOAL_LEARN_PATTERNS     = "learn_outage_patterns"         # priority 5
    GOAL_VISIBILITY         = "maintain_user_visibility"      # priority 6

    # ── Decision thresholds ────────────────────────────────────────────────
    FUEL_CRITICAL_PCT = 20.0   # Plan D activates below this
    FUEL_LOW_PCT      = 30.0   # Plan B withholds generator below this
    ALERT_WINDOW_HRS  = 2.0    # Warn user this many hours before predicted cut
    WARN_WINDOW_MIN   = 20     # Urgent warning this many minutes before cut

    def __init__(self, jid: str, password: str, environment: Environment,
                 notifier_jid: str = None):
        super().__init__(jid, password)
        self.environment = environment
        self.notifier_jid = notifier_jid   # JID of the NotifierAgent

        # Belief base — agent's internal world model
        self.beliefs: dict = {
            "power_on":          True,
            "was_power_on":      True,
            "generator_running": False,
            "fuel_pct":          environment.fuel_pct,
            "fuel_litres":       environment.fuel_litres,
            "fuel_runtime_hrs":  0.0,
            "hour":              environment.sim_time.hour,
            "is_waking_hours":   True,
            "outage_count":      0,
            "outage_history":    [],
            "last_updated":      None,
        }

        # Active plan tracker
        self.active_plan: str = "A"

        # Session statistics
        self.stats: dict = {
            "generator_starts": 0,
            "alerts_sent":      0,
            "loads_shed":       0,
            "fuel_warnings":    0,
        }

        # Alert log (most recent 50)
        self.alert_log: list = []

    async def setup(self):
        """
        Called by SPADE when the agent starts.
        Registers Plan A (normal monitoring) as the always-running behaviour.
        Other plans are added reactively when events are detected.
        """
        log(self.name, "Dumsor Guard agent initialised", "OK")
        log(self.name, f"Goals: {self.GOAL_PROTECT_CRITICAL}, {self.GOAL_MINIMISE_FUEL}", "INFO")
        log(self.name, f"Starting fuel: {self.environment.fuel_pct:.0f}%", "INFO")
        log(self.name, "Plan A (normal monitoring) registered — polling every 5s", "INFO")

        # Register Plan A as a periodic background behaviour (every 5 seconds)
        monitoring = NormalMonitoringBehaviour(period=5)
        self.add_behaviour(monitoring)

    # -----------------------------------------------------------------------
    # Agent utility methods
    # -----------------------------------------------------------------------

    def send_alert(self, message: str, level: str = "INFO"):
        """
        Send an alert to the NotifierAgent via SPADE ACL messaging.

        The DumsorAgent's job ends here — it composes the alert and
        hands it off. The NotifierAgent is solely responsible for
        formatting and delivering it to the homeowner.

        In SPADE, messages are sent asynchronously. We schedule the
        coroutine on the agent's event loop so send_alert() can be
        called from both sync and async contexts.
        """
        self.stats["alerts_sent"] += 1

        # Keep local log for summary (agent's own record)
        entry = {
            "time":    datetime.now().isoformat(),
            "message": message,
            "level":   level,
        }
        self.alert_log.append(entry)
        if len(self.alert_log) > 50:
            self.alert_log = self.alert_log[-50:]

        if self.notifier_jid:
            # ── Build SPADE ACL message ────────────────────────────────────
            msg = Message(to=self.notifier_jid)
            msg.set_metadata("performative", "inform")
            msg.set_metadata("ontology",     "dumsor-alert")
            msg.body = json.dumps({"message": message, "level": level})

            # Schedule async send on the agent's running loop
            asyncio.ensure_future(self._async_send(msg))
            log(self.name,
                f"Alert dispatched to NotifierAgent [{level}]: {message[:60]}",
                "ACTION")
        else:
            # Fallback: no notifier configured — print directly
            print(f"\n  {'='*56}")
            print(f"  📱 ALERT [{level}]: {message}")
            print(f"  {'='*56}\n")

    async def _async_send(self, msg: Message):
        """Coroutine wrapper for SPADE's async send."""
        await self.send(msg)

    def _predict_outage_probability(self) -> int:
        """
        Simple pattern-based outage probability estimate (%).
        In production this would use a proper time-series model.
        Uses frequency of past outages as a proxy.
        """
        history = self.beliefs.get("outage_history", [])
        if len(history) == 0:
            return 8
        elif len(history) <= 2:
            return 15
        elif len(history) <= 5:
            return 35
        else:
            return min(85, 35 + len(history) * 5)

    def _predict_outage_duration(self) -> float:
        """
        Predict how long the current/next outage will last (hours).
        Based on average of last 5 outages, defaults to 3.0hrs.
        """
        history = self.beliefs.get("outage_history", [])
        if not history:
            return 3.0
        recent = history[-5:]
        durations = [r["duration_hrs"] for r in recent if r["duration_hrs"] > 0]
        if not durations:
            return 3.0
        return round(sum(durations) / len(durations), 2)

    def print_summary(self):
        """Print a full agent session summary."""
        print("\n" + "="*60)
        print("  DUMSOR GUARD — SESSION SUMMARY")
        print("="*60)
        print(f"  Active plan       : Plan {self.active_plan}")
        print(f"  Generator starts  : {self.stats['generator_starts']}")
        print(f"  Alerts sent       : {self.stats['alerts_sent']}")
        print(f"  Loads shed        : {self.stats['loads_shed']}")
        print(f"  Fuel warnings     : {self.stats['fuel_warnings']}")
        print(f"  Fuel consumed     : {self.environment.fuel_consumed_L:.3f}L")
        print(f"  Total cost        : ₵{self.environment.total_cost_GHS:.2f}")
        print(f"  Total outages     : {self.environment.outage_count}")
        print("-"*60)
        print("  RECENT ALERTS:")
        for alert in self.alert_log[-5:]:
            t = alert['time'][11:19]
            print(f"    [{t}] [{alert['level']}] {alert['message']}")
        print("="*60)