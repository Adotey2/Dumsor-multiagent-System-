"""
notifier_agent.py
=================
Dumsor Guard — Notifier Agent (XMPP-enabled)

XMPP JID  : dumsor_notifier@xmpp.jp
Listens for ACL messages from dumsor_guard@xmpp.jp
"""

import json
from datetime import datetime

from spade.agent import Agent
from spade.behaviour import CyclicBehaviour
from spade.template import Template


def log(name: str, msg: str, level: str = "INFO"):
    icons = {"INFO": "·", "WARN": "⚠", "CRIT": "✖", "OK": "✓", "ACTION": "▶"}
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] [{name}] {icons.get(level,'·')} [{level}] {msg}")


# ===========================================================================
# Alert Listener Behaviour
# ===========================================================================

class AlertListenerBehaviour(CyclicBehaviour):
    async def run(self):
        msg = await self.receive(timeout=20)
        if msg is None:
            return

        agent = self.agent
        try:
            payload = json.loads(msg.body)
            message = payload.get("message", "")
            level   = payload.get("level", "INFO")
        except (json.JSONDecodeError, AttributeError, TypeError):
            log(agent.name,
                f"Malformed message from {msg.sender}: {msg.body}", "WARN")
            return

        print(agent._format_alert(message, level))

        agent.delivery_log.append({
            "time":    datetime.now().strftime("%H:%M:%S"),
            "from":    str(msg.sender),
            "level":   level,
            "message": message,
        })
        agent.stats["delivered"] += 1
        if level == "CRIT":
            agent.stats["critical"] += 1
        elif level == "WARN":
            agent.stats["warnings"] += 1
        else:
            agent.stats["info"] += 1

        log(agent.name,
            f"Alert delivered | [{level}] | Total: {agent.stats['delivered']}",
            "ACTION")


# ===========================================================================
# NotifierAgent
# ===========================================================================

class NotifierAgent(Agent):
    """
    Connects to xmpp.jp and listens for alerts from DumsorAgent.
    Production channel: SMS via Arkesel / Hubtel (Ghana).
    """

    def __init__(self, jid: str, password: str,
                 verify_security: bool = True):
        super().__init__(jid, password, verify_security=verify_security)
        self.delivery_log: list = []
        self.stats: dict = {
            "delivered": 0,
            "critical":  0,
            "warnings":  0,
            "info":      0,
        }

    async def setup(self):
        log(self.name, f"NotifierAgent online — connected to xmpp.jp", "OK")
        log(self.name,
            "Listening for alerts from dumsor_guard@xmpp.jp", "INFO")
        log(self.name,
            "Production channel: SMS via Arkesel / Hubtel (Ghana)", "INFO")

        template = Template()
        template.set_metadata("ontology", "dumsor-alert")
        self.add_behaviour(AlertListenerBehaviour(), template)

    def _format_alert(self, message: str, level: str) -> str:
        now       = datetime.now().strftime("%A %d %b %Y  %H:%M:%S")
        level_key = level.upper()

        words, lines, cur = message.split(), [], ""
        for w in words:
            if len(cur) + len(w) + 1 <= 52:
                cur = (cur + " " + w).strip()
            else:
                lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        lines = lines or [""]

        if level_key == "CRIT":
            b    = "═" * 56
            out  = f"\n  ╔{b}╗\n"
            out += f"  ║  🚨  CRITICAL ALERT  {'':32}║\n"
            for ln in lines:
                out += f"  ║  {ln:<54}  ║\n"
            out += f"  ║  {now:<54}  ║\n"
            out += f"  ╚{b}╝\n"
        elif level_key == "WARN":
            b    = "─" * 56
            out  = f"\n  ┌{b}┐\n"
            out += f"  │  ⚠   WARNING  {'':40}│\n"
            for ln in lines:
                out += f"  │  {ln:<54}  │\n"
            out += f"  │  {now:<54}  │\n"
            out += f"  └{b}┘\n"
        elif level_key == "OK":
            b    = "─" * 56
            out  = f"\n  ┌{b}┐\n"
            out += f"  │  ✓   RESOLVED  {'':40}│\n"
            for ln in lines:
                out += f"  │  {ln:<54}  │\n"
            out += f"  │  {now:<54}  │\n"
            out += f"  └{b}┘\n"
        else:
            b    = "─" * 56
            out  = f"\n  ┌{b}┐\n"
            out += f"  │  ·   INFO  {'':44}│\n"
            for ln in lines:
                out += f"  │  {ln:<54}  │\n"
            out += f"  │  {now:<54}  │\n"
            out += f"  └{b}┘\n"
        return out

    def print_summary(self):
        print("\n" + "="*60)
        print("  NOTIFIER AGENT — DELIVERY SUMMARY")
        print("="*60)
        print(f"  XMPP JID               : {self.jid}")
        print(f"  Total alerts delivered : {self.stats['delivered']}")
        print(f"  Critical               : {self.stats['critical']}")
        print(f"  Warnings               : {self.stats['warnings']}")
        print(f"  Info / OK              : {self.stats['info']}")
        print("─"*60)
        print("  FULL DELIVERY LOG:")
        for e in self.delivery_log:
            print(f"    [{e['time']}] [{e['level']:<4}] "
                  f"[from: {e['from'][:25]}] {e['message'][:45]}")
        print("="*60)