"""
dumsor_agent.py
===============
Dumsor Guard — SPADE Agent (XMPP-enabled)

Connects to xmpp.jp public XMPP server.
Communicates with NotifierAgent via genuine SPADE ACL messages.

XMPP Message Protocol
---------------------
  From  : dumsor_guard@xmpp.jp
  To    : dumsor_notifier@xmpp.jp
  Body  : JSON  { "message": str, "level": "INFO|WARN|CRIT|OK" }
  Meta  : performative=inform, ontology=dumsor-alert
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

def log(name: str, msg: str, level: str = "INFO"):
    icons = {"INFO": "·", "WARN": "⚠", "CRIT": "✖", "OK": "✓", "ACTION": "▶"}
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] [{name}] {icons.get(level,'·')} [{level}] {msg}")


# ===========================================================================
# PLAN A — Normal Monitoring Behaviour
# ===========================================================================

class NormalMonitoringBehaviour(PeriodicBehaviour):
    async def run(self):
        agent = self.agent
        env: Environment = agent.environment
        p = env.percepts

        agent.beliefs.update({
            "power_on":          p["power_on"],
            "generator_running": p["generator_running"],
            "fuel_pct":          p["fuel_pct"],
            "fuel_litres":       p["fuel_litres"],
            "fuel_runtime_hrs":  p["fuel_runtime_hrs"],
            "hour":              p["hour"],
            "is_waking_hours":   p["is_waking_hours"],
            "outage_count":      p["outage_count"],
            "outage_history":    p["outage_history"],
            "ecg_schedule":      p["ecg_schedule"],
            "last_updated":      datetime.now().isoformat(),
        })

        if p["fuel_pct"] < agent.FUEL_CRITICAL_PCT and agent.active_plan != "D":
            log(agent.name, f"Fuel {p['fuel_pct']:.0f}% critical → Plan D", "CRIT")
            agent.active_plan = "D"
            self.agent.add_behaviour(FuelConservationBehaviour())

        elif (not p["power_on"]
              and agent.beliefs.get("was_power_on", True)
              and agent.active_plan not in ("B", "C", "D")):
            if p["scheduled_cut"]:
                log(agent.name, "Scheduled cut detected → Plan C", "WARN")
                agent.active_plan = "C"
                self.agent.add_behaviour(ScheduledCutBehaviour())
            else:
                log(agent.name, "Unannounced cut detected → Plan B", "CRIT")
                agent.active_plan = "B"
                self.agent.add_behaviour(UnannouncedCutBehaviour())

        elif (p["power_on"]
              and not agent.beliefs.get("was_power_on", True)
              and agent.active_plan != "A"):
            log(agent.name, "Power restored → PowerRestored plan", "OK")
            agent.active_plan = "A"
            self.agent.add_behaviour(PowerRestoredBehaviour())

        else:
            if agent.active_plan == "A":
                for (start, end) in p["ecg_schedule"]:
                    hrs_away = start - p["hour"]
                    if 0 < hrs_away <= 2:
                        await agent.send_alert(
                            f"Scheduled ECG cut at {start:02d}:00 "
                            f"({hrs_away}hrs away). Fuel: {p['fuel_pct']:.0f}%.",
                            "WARN"
                        )
                prob = agent._predict_outage_probability()
                log(agent.name,
                    f"Plan A | Power: ON | Fuel: {p['fuel_pct']:.0f}% "
                    f"| Prob: {prob}% | {p['hour']:02d}:00", "INFO")

        agent.beliefs["was_power_on"] = p["power_on"]


# ===========================================================================
# PLAN B — Unannounced Cut Behaviour
# ===========================================================================

class UnannouncedCutBehaviour(OneShotBehaviour):
    async def run(self):
        agent = self.agent
        env   = agent.environment
        p     = env.percepts

        log(agent.name, "Plan B — unannounced cut response", "ACTION")
        fuel   = p["fuel_pct"]
        waking = p["is_waking_hours"]
        hour   = p["hour"]

        if fuel < agent.FUEL_CRITICAL_PCT:
            log(agent.name, "Fuel critical → escalating to Plan D", "CRIT")
            agent.active_plan = "D"
            self.agent.add_behaviour(FuelConservationBehaviour())
            return

        if waking and fuel >= agent.FUEL_LOW_PCT:
            started = env.start_generator()
            if started:
                agent.stats["generator_starts"] += 1
                shed = env.shed_non_critical()
                agent.stats["loads_shed"] += len(shed)
                pred = agent._predict_outage_duration()
                log(agent.name, f"Generator STARTED | Shed: {', '.join(shed)}", "ACTION")
                await agent.send_alert(
                    f"Unannounced power cut! Generator ON. "
                    f"Non-critical loads shed. Est. {pred:.1f}hrs "
                    f"(~{pred * env.burn_rate_Lph:.1f}L fuel). "
                    f"Fridge and router protected.",
                    "CRIT"
                )
        elif not waking:
            shed = env.shed_non_critical()
            agent.stats["loads_shed"] += len(shed)
            log(agent.name, "Night cut — generator withheld", "WARN")
            await agent.send_alert(
                f"Power cut at {hour:02d}:00 (night). "
                f"Generator held — saves fuel. Fridge on backup. Loads shed.",
                "WARN"
            )
        else:
            shed = env.shed_non_critical()
            agent.stats["loads_shed"] += len(shed)
            log(agent.name, f"Fuel low ({fuel:.0f}%) — generator withheld", "WARN")
            await agent.send_alert(
                f"Power cut! Fuel too low ({fuel:.0f}%) to start generator. "
                f"Loads shed. Refuel soon.",
                "WARN"
            )


# ===========================================================================
# PLAN C — Scheduled Cut Behaviour
# ===========================================================================

class ScheduledCutBehaviour(OneShotBehaviour):
    MIN_DURATION_HRS = 0.75

    async def run(self):
        agent = self.agent
        env   = agent.environment
        p     = env.percepts

        log(agent.name, "Plan C — scheduled cut management", "ACTION")
        fuel = p["fuel_pct"]

        if fuel < agent.FUEL_CRITICAL_PCT:
            log(agent.name, "Fuel critical → escalating to Plan D", "CRIT")
            agent.active_plan = "D"
            self.agent.add_behaviour(FuelConservationBehaviour())
            return

        pred     = agent._predict_outage_duration()
        fuel_L   = pred * env.burn_rate_Lph
        cost_est = fuel_L * env.fuel_price_GHS

        if pred < self.MIN_DURATION_HRS:
            log(agent.name,
                f"Predicted {pred:.1f}hrs < 45min — skipping generator", "INFO")
            await agent.send_alert(
                f"Scheduled cut. ~{pred*60:.0f}min predicted — "
                f"too short for generator. Conserving fuel.", "INFO")
            return

        started = env.start_generator()
        if started:
            agent.stats["generator_starts"] += 1
            shed = env.shed_non_critical()
            agent.stats["loads_shed"] += len(shed)
            log(agent.name,
                f"Generator STARTED | Est: ₵{cost_est:.2f} | "
                f"Shed: {', '.join(shed)}", "ACTION")
            await agent.send_alert(
                f"Scheduled cut. Generator ON. Est. {pred:.1f}hrs — "
                f"~{fuel_L:.1f}L fuel (₵{cost_est:.2f}). "
                f"Non-critical loads shed.",
                "WARN"
            )


# ===========================================================================
# PLAN D — Fuel Conservation Behaviour
# ===========================================================================

class FuelConservationBehaviour(OneShotBehaviour):
    async def run(self):
        agent = self.agent
        env   = agent.environment
        p     = env.percepts

        log(agent.name, "Plan D — FUEL CONSERVATION ACTIVATED", "CRIT")
        agent.stats["fuel_warnings"] += 1

        shed    = env.shed_non_critical()
        agent.stats["loads_shed"] += len(shed)
        fuel_L  = p["fuel_litres"]
        runtime = fuel_L / env.burn_rate_Lph if fuel_L > 0 else 0

        log(agent.name,
            f"Fuel: {fuel_L:.2f}L | Runtime: ~{runtime:.1f}hrs | "
            f"Shed: {', '.join(shed) if shed else 'already shed'}", "CRIT")

        await agent.send_alert(
            f"FUEL CRITICAL: {p['fuel_pct']:.0f}% remaining "
            f"({fuel_L:.1f}L). Est. {runtime:.1f}hrs left. "
            f"Generator withheld. ALL non-critical loads off. "
            f"REFUEL IMMEDIATELY.",
            "CRIT"
        )


# ===========================================================================
# Power Restored Behaviour
# ===========================================================================

class PowerRestoredBehaviour(OneShotBehaviour):
    async def run(self):
        agent = self.agent
        env   = agent.environment

        if env.generator_running:
            env.stop_generator()
            log(agent.name, "Generator STOPPED", "ACTION")

        env.restore_all_loads()
        log(agent.name, "All loads RESTORED — Plan A", "OK")

        history = env.percepts["outage_history"]
        if history:
            last = history[-1]
            log(agent.name,
                f"Outage: {last['duration_hrs']:.2f}hrs | "
                f"Fuel used: {env.fuel_consumed_L:.2f}L | "
                f"Cost: ₵{env.total_cost_GHS:.2f}", "OK")

        await agent.send_alert(
            f"ECG power restored. Generator off. All loads online. "
            f"Fuel: {env.fuel_litres:.2f}L ({env.fuel_pct:.1f}%). "
            f"Week cost: ₵{env.total_cost_GHS:.2f}.",
            "OK"
        )


# ===========================================================================
# DumsorAgent — Main SPADE Agent class
# ===========================================================================

class DumsorAgent(Agent):
    """
    Dumsor Guard — Intelligent Home Power Management Agent.
    Connects to xmpp.jp. Sends alerts to dumsor_notifier@xmpp.jp.
    """

    GOAL_PROTECT_CRITICAL = "protect_critical_appliances"
    GOAL_WARN_USER        = "warn_user_before_disruption"
    GOAL_MINIMISE_FUEL    = "minimise_fuel_consumption"
    GOAL_PROTECT_RESERVES = "protect_fuel_reserves"
    GOAL_LEARN_PATTERNS   = "learn_outage_patterns"
    GOAL_VISIBILITY       = "maintain_user_visibility"

    FUEL_CRITICAL_PCT = 20.0
    FUEL_LOW_PCT      = 30.0

    def __init__(self, jid: str, password: str,
                 environment: Environment, notifier_jid: str,
                 verify_security: bool = True):
        super().__init__(jid, password, verify_security=verify_security)
        self.environment  = environment
        self.notifier_jid = notifier_jid
        self.active_plan  = "A"

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
            "ecg_schedule":      [],
            "last_updated":      None,
        }
        self.stats: dict = {
            "generator_starts": 0,
            "alerts_sent":      0,
            "loads_shed":       0,
            "fuel_warnings":    0,
        }
        self.alert_log: list = []

    async def setup(self):
        log(self.name, f"DumsorAgent online — connected to xmpp.jp", "OK")
        log(self.name,
            f"Goals: G1-protect_critical → G6-visibility | "
            f"Fuel: {self.environment.fuel_pct:.0f}%", "INFO")
        self.add_behaviour(NormalMonitoringBehaviour(period=3))

    async def send_alert(self, message: str, level: str = "INFO"):
        self.stats["alerts_sent"] += 1
        self.alert_log.append({
            "time":    datetime.now().isoformat(),
            "level":   level,
            "message": message,
        })
        if len(self.alert_log) > 50:
            self.alert_log = self.alert_log[-50:]

        msg = Message(to=self.notifier_jid)
        msg.set_metadata("performative", "inform")
        msg.set_metadata("ontology",     "dumsor-alert")
        msg.body = json.dumps({"message": message, "level": level})

        await self.send(msg)
        log(self.name,
            f"→ XMPP({self.notifier_jid}) [{level}]: {message[:50]}", "ACTION")

    def _predict_outage_probability(self) -> int:
        n = len(self.beliefs.get("outage_history", []))
        return min(85, [8, 15, 25, 35, 50, 65, 75, 85][min(n, 7)])

    def _predict_outage_duration(self) -> float:
        history = self.beliefs.get("outage_history", [])
        recent  = [r["duration_hrs"] for r in history[-5:] if r["duration_hrs"] > 0]
        return round(sum(recent) / len(recent), 2) if recent else 3.0

    def print_summary(self):
        print("\n" + "="*60)
        print("  DUMSOR GUARD — SESSION SUMMARY")
        print("="*60)
        print(f"  Active plan       : Plan {self.active_plan}")
        print(f"  XMPP JID          : {self.jid}")
        print(f"  Notifier JID      : {self.notifier_jid}")
        print(f"  Generator starts  : {self.stats['generator_starts']}")
        print(f"  Alerts sent       : {self.stats['alerts_sent']}")
        print(f"  Loads shed        : {self.stats['loads_shed']}")
        print(f"  Fuel warnings     : {self.stats['fuel_warnings']}")
        print(f"  Fuel consumed     : {self.environment.fuel_consumed_L:.2f}L")
        print(f"  Total cost        : ₵{self.environment.total_cost_GHS:.2f}")
        print(f"  Total outages     : {self.environment.outage_count}")
        print("─"*60)
        print("  RECENT ALERTS (sent via xmpp.jp):")
        for a in self.alert_log[-5:]:
            t = a["time"][11:19]
            print(f"    [{t}] [{a['level']:<4}] {a['message'][:55]}")
        print("="*60)