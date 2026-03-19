"""
notifier_agent.py
=================
Dumsor Guard — Notifier Agent

Prometheus Role  : User communication specialist
Responsibility   : Receive alert messages from DumsorAgent via SPADE ACL
                   messaging, format them, and deliver them to the homeowner.

Why a separate agent?
---------------------
In Prometheus, each agent should have a single, clearly bounded role.
The DumsorAgent's role is to perceive the environment and make decisions.
Delivery of notifications to the user is a separate concern — it has its
own logic (formatting, deduplication, urgency escalation, channel routing)
that would pollute the decision agent if kept there.

In a real deployment, this agent would:
  - Send SMS via Hubtel or Arkesel (Ghanaian SMS APIs)
  - Push to a mobile app
  - Trigger a WhatsApp message

In this simulation it formats and prints to console, and maintains
a full delivery log that can be inspected at the end.

Message Protocol (SPADE ACL)
-----------------------------
DumsorAgent sends messages to NotifierAgent using SPADE's messaging system.
Message body is a JSON string with fields:
  {
    "message": "Alert text here",
    "level":   "INFO" | "WARN" | "CRIT" | "OK"
  }

NotifierAgent listens with a CyclicBehaviour, parses each message,
formats it by level, prints it, and logs it.
"""

import json
from datetime import datetime

import spade
from spade.agent import Agent
from spade.behaviour import CyclicBehaviour
from spade.message import Message


# ---------------------------------------------------------------------------
# Logging helper (shared style with dumsor_agent.py)
# ---------------------------------------------------------------------------

def log(agent_name: str, msg: str, level: str = "INFO"):
    icons = {"INFO": "·", "WARN": "⚠", "CRIT": "✖", "OK": "✓", "ACTION": "▶"}
    icon = icons.get(level, "·")
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] [{agent_name}] {icon} [{level}] {msg}")


# ===========================================================================
# Alert Listener Behaviour
# Runs continuously, waiting for messages from DumsorAgent
# ===========================================================================

class AlertListenerBehaviour(CyclicBehaviour):
    """
    Prometheus Plan: Listen and Deliver

    A CyclicBehaviour that blocks waiting for an incoming SPADE message.
    When a message arrives from DumsorAgent, it:
      1. Parses the JSON payload
      2. Formats the alert based on urgency level
      3. Prints it to the console (simulating SMS/push delivery)
      4. Logs it to the agent's delivery log
    """

    async def run(self):
        # Block and wait for the next incoming message (timeout 10s)
        msg = await self.receive(timeout=10)

        if msg is None:
            # No message this cycle — agent is idle, nothing to do
            return

        agent = self.agent

        # ── Parse message body ─────────────────────────────────────────────
        try:
            payload = json.loads(msg.body)
            message = payload.get("message", "")
            level   = payload.get("level", "INFO")
        except (json.JSONDecodeError, AttributeError):
            log(agent.name, f"Malformed message received: {msg.body}", "WARN")
            return

        # ── Format by urgency level ────────────────────────────────────────
        formatted = agent._format_alert(message, level)

        # ── Deliver to user ────────────────────────────────────────────────
        print(formatted)

        # ── Log to delivery record ─────────────────────────────────────────
        agent.delivery_log.append({
            "time":    datetime.now().isoformat(),
            "from":    str(msg.sender),
            "message": message,
            "level":   level,
        })

        agent.stats["delivered"] += 1
        log(agent.name,
            f"Alert delivered to homeowner | Level: {level} | "
            f"Total delivered: {agent.stats['delivered']}",
            "ACTION")


# ===========================================================================
# The Notifier Agent
# ===========================================================================

class NotifierAgent(Agent):
    """
    Notifier Agent — User Communication Specialist.

    Prometheus role : Handle all homeowner-facing communication
    Percepts        : Incoming SPADE ACL messages from DumsorAgent
    Actions         : Format and deliver alerts to the homeowner
    Goals           : Ensure every alert reaches the user promptly
                      and in a readable, actionable format
    Beliefs         : Delivery log, alert history, stats

    Parameters
    ----------
    jid      : XMPP Jabber ID (e.g. notifier@localhost)
    password : XMPP password
    """

    # Alert format templates per level
    FORMATS = {
        "CRIT": (
            "\n  ╔{'═'*54}╗\n"
            "  ║  🚨 CRITICAL ALERT                                    ║\n"
            "  ║  {msg:<52}  ║\n"
            "  ║  {time:<52}  ║\n"
            "  ╚{'═'*54}╝\n"
        ),
        "WARN": (
            "\n  ┌{'─'*54}┐\n"
            "  │  ⚠  WARNING: {msg:<44}  │\n"
            "  │  {time:<54}  │\n"
            "  └{'─'*54}┘\n"
        ),
        "OK": (
            "\n  ┌{'─'*54}┐\n"
            "  │  ✓  {msg:<50}  │\n"
            "  │  {time:<54}  │\n"
            "  └{'─'*54}┘\n"
        ),
        "INFO": (
            "\n  ┌{'─'*54}┐\n"
            "  │  ·  {msg:<50}  │\n"
            "  │  {time:<54}  │\n"
            "  └{'─'*54}┘\n"
        ),
    }

    def __init__(self, jid: str, password: str):
        super().__init__(jid, password)

        # Delivery log — full record of every alert sent to homeowner
        self.delivery_log: list = []

        # Stats
        self.stats: dict = {
            "delivered":  0,
            "critical":   0,
            "warnings":   0,
            "info":       0,
        }

    async def setup(self):
        log(self.name, "Notifier Agent online — listening for alerts from DumsorAgent", "OK")
        log(self.name, "Delivery channel: console (production: SMS via Arkesel/Hubtel)", "INFO")

        # Register the single behaviour — listen and deliver
        listener = AlertListenerBehaviour()
        self.add_behaviour(listener)

    # -----------------------------------------------------------------------
    # Alert formatting
    # -----------------------------------------------------------------------

    def _format_alert(self, message: str, level: str) -> str:
        """
        Format an alert message for display to the homeowner.
        Uses level-specific visual styling so critical alerts
        are immediately obvious at a glance.
        """
        now = datetime.now().strftime("%A %d %b %Y  %H:%M:%S")

        # Update level stats
        level_key = level.upper()
        if level_key == "CRIT":
            self.stats["critical"] += 1
        elif level_key == "WARN":
            self.stats["warnings"] += 1
        else:
            self.stats["info"] += 1

        # Wrap long messages at 50 chars
        words = message.split()
        lines = []
        current = ""
        for word in words:
            if len(current) + len(word) + 1 <= 50:
                current = current + (" " if current else "") + word
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)

        if level_key == "CRIT":
            border = "═" * 56
            output  = f"\n  ╔{border}╗\n"
            output += f"  ║  🚨  CRITICAL ALERT  {'':32}║\n"
            for line in lines:
                output += f"  ║  {line:<54}  ║\n"
            output += f"  ║  {now:<54}  ║\n"
            output += f"  ╚{border}╝\n"
        elif level_key == "WARN":
            border = "─" * 56
            output  = f"\n  ┌{border}┐\n"
            output += f"  │  ⚠   WARNING  {'':40}│\n"
            for line in lines:
                output += f"  │  {line:<54}  │\n"
            output += f"  │  {now:<54}  │\n"
            output += f"  └{border}┘\n"
        elif level_key == "OK":
            border = "─" * 56
            output  = f"\n  ┌{border}┐\n"
            output += f"  │  ✓   RESOLVED  {'':40}│\n"
            for line in lines:
                output += f"  │  {line:<54}  │\n"
            output += f"  │  {now:<54}  │\n"
            output += f"  └{border}┘\n"
        else:
            border = "─" * 56
            output  = f"\n  ┌{border}┐\n"
            output += f"  │  ·   INFO  {'':44}│\n"
            for line in lines:
                output += f"  │  {line:<54}  │\n"
            output += f"  │  {now:<54}  │\n"
            output += f"  └{border}┘\n"

        return output

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------

    def print_summary(self):
        """Print delivery statistics at end of simulation."""
        print("\n" + "="*60)
        print("  NOTIFIER AGENT — DELIVERY SUMMARY")
        print("="*60)
        print(f"  Total alerts delivered : {self.stats['delivered']}")
        print(f"  Critical alerts        : {self.stats['critical']}")
        print(f"  Warnings               : {self.stats['warnings']}")
        print(f"  Info / OK              : {self.stats['info']}")
        print("-"*60)
        print("  FULL DELIVERY LOG:")
        for entry in self.delivery_log:
            t = entry['time'][11:19]
            lvl = entry['level']
            msg = entry['message'][:60]
            print(f"    [{t}] [{lvl:<4}] {msg}")
        print("="*60)