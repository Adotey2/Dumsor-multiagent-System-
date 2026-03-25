"""
environment.py
==============
Dumsor Guard — Simulated Environment

Represents the physical world the agent perceives and acts upon:
  - ECG power grid (on/off, scheduled/unannounced cuts)
  - Generator (running state, fuel level, burn rate)
  - Appliances (priority-ranked, sheddable)
  - Clock (simulated time of day)
  - Outage history (used by agent for pattern learning)

This module is intentionally decoupled from the agent so the
Prometheus percept/action boundary is clean and explicit.
"""

from dataclasses import dataclass, field
from typing import Optional
import datetime
import random


# ---------------------------------------------------------------------------
# Appliance definition
# ---------------------------------------------------------------------------

@dataclass
class Appliance:
    """
    Represents a single household appliance.

    Attributes
    ----------
    name        : Human-readable name
    watt        : Power draw in watts
    priority    : 1 = most critical (never shed), higher = shed first
    critical    : If True, agent will burn fuel to keep this on
    on          : Current power state
    shed        : True if agent has deliberately switched it off to save fuel
    """
    name: str
    watt: int
    priority: int
    critical: bool
    on: bool = True
    shed: bool = False

    def __str__(self):
        state = "SHED" if self.shed else ("ON" if self.on else "OFF")
        crit = " [CRITICAL]" if self.critical else ""
        return f"{self.name:<20} {self.watt:>4}W  priority={self.priority}  {state}{crit}"


# ---------------------------------------------------------------------------
# Outage record
# ---------------------------------------------------------------------------

@dataclass
class OutageRecord:
    """
    Logged record of a single past outage.
    Used by the agent to learn patterns and improve predictions.
    """
    start_time: datetime.datetime
    end_time: Optional[datetime.datetime] = None
    announced: bool = False
    duration_hours: float = 0.0

    def close(self, end_time: datetime.datetime):
        self.end_time = end_time
        delta = end_time - self.start_time
        self.duration_hours = delta.total_seconds() / 3600


# ---------------------------------------------------------------------------
# Environment (the world)
# ---------------------------------------------------------------------------

class Environment:
    """
    Simulated environment for the Dumsor Guard agent.

    The agent reads percepts from this object and calls action methods on it.
    Nothing in here makes decisions — that is the agent's job.

    Parameters
    ----------
    fuel_pct        : Starting fuel level as a percentage (0–100)
    start_hour      : Simulated starting hour of day (0–23)
    fuel_capacity_L : Total tank capacity in litres
    burn_rate_Lph   : Generator fuel consumption in litres per hour
    fuel_price_GHS  : Price of fuel in Ghanaian cedis per litre
    """

    # Default appliances ranked by priority (1 = most critical)
    DEFAULT_APPLIANCES = [
        Appliance("Fridge",         150, priority=1, critical=True),
        Appliance("Home router",     20, priority=2, critical=True),
        Appliance("Lights",          60, priority=3, critical=False),
        Appliance("Ceiling fan",     75, priority=4, critical=False),
        Appliance("Laptop charger",  65, priority=5, critical=False),
        Appliance("Television",     120, priority=6, critical=False),
        Appliance("Air conditioner",900, priority=7, critical=False),
    ]

    def __init__(
        self,
        fuel_pct: float = 78.0,
        start_hour: int = 6,
        fuel_capacity_L: float = 10.0,
        burn_rate_Lph: float = 1.0,
        fuel_price_GHS: float = 14.5,
    ):
        # --- Power grid state ---
        self.power_on: bool = True
        self.scheduled_cut: bool = False       # Was the current outage announced?

        # --- Generator ---
        self.generator_running: bool = False
        self.fuel_pct: float = fuel_pct        # 0.0 – 100.0
        self.fuel_capacity_L: float = fuel_capacity_L
        self.burn_rate_Lph: float = burn_rate_Lph
        self.fuel_price_GHS: float = fuel_price_GHS
        self.fuel_consumed_L: float = 0.0
        self.total_cost_GHS: float = 0.0

        # --- Clock ---
        self.sim_time: datetime.datetime = datetime.datetime.now().replace(
            hour=start_hour, minute=0, second=0, microsecond=0
        )

        # --- Appliances ---
        self.appliances: list[Appliance] = [
            Appliance(
                a.name, a.watt, a.priority, a.critical
            )
            for a in self.DEFAULT_APPLIANCES
        ]

        # --- History & stats ---
        self.outage_history: list[OutageRecord] = []
        self.current_outage: Optional[OutageRecord] = None
        self.outage_count: int = 0
        self.generator_starts: int = 0

        # --- ECG schedule (announced cuts as hour ranges) ---
        # Format: list of (start_hour, end_hour) tuples
        self.ecg_schedule: list[tuple] = []

    # -----------------------------------------------------------------------
    # Percept interface — agent reads these
    # -----------------------------------------------------------------------

    @property
    def percepts(self) -> dict:
        """
        Returns all current environment observations as a dictionary.
        This is the formal percept set the agent receives each cycle.
        """
        fuel_L = self.fuel_litres
        return {
            "power_on":          self.power_on,
            "scheduled_cut":     self.scheduled_cut,
            "generator_running": self.generator_running,
            "fuel_pct":          round(self.fuel_pct, 2),
            "fuel_litres":       round(fuel_L, 2),
            "fuel_runtime_hrs":  round(fuel_L / self.burn_rate_Lph, 2) if fuel_L > 0 else 0.0,
            "hour":              self.sim_time.hour,
            "minute":            self.sim_time.minute,
            "is_waking_hours":   6 <= self.sim_time.hour < 23,
            "appliances":        {a.name: {"on": a.on, "shed": a.shed, "watt": a.watt, "critical": a.critical}
                                  for a in self.appliances},
            "outage_count":      self.outage_count,
            "outage_history":    [
                {"duration_hrs": r.duration_hours, "announced": r.announced}
                for r in self.outage_history[-10:]   # last 10 outages
            ],
            "ecg_schedule":      self.ecg_schedule,
            "fuel_consumed_L":   round(self.fuel_consumed_L, 3),
            "total_cost_GHS":    round(self.total_cost_GHS, 2),
        }

    @property
    def fuel_litres(self) -> float:
        return (self.fuel_pct / 100.0) * self.fuel_capacity_L

    # -----------------------------------------------------------------------
    # Action interface — agent calls these
    # -----------------------------------------------------------------------

    def start_generator(self) -> bool:
        """
        Start the generator.
        Returns True if successful, False if already running or no fuel.
        """
        if self.generator_running:
            return False
        if self.fuel_pct <= 0:
            return False
        self.generator_running = True
        self.generator_starts += 1
        return True

    def stop_generator(self) -> bool:
        """Stop the generator. Returns True if it was running."""
        if not self.generator_running:
            return False
        self.generator_running = False
        return True

    def shed_load(self, appliance_name: str) -> bool:
        """
        Switch off a named appliance to conserve fuel.
        Returns True if the appliance was found and shed.
        """
        for a in self.appliances:
            if a.name.lower() == appliance_name.lower():
                if not a.shed:
                    a.shed = True
                    a.on = False
                    return True
        return False

    def restore_load(self, appliance_name: str) -> bool:
        """
        Restore a previously shed appliance.
        Returns True if found and restored.
        """
        for a in self.appliances:
            if a.name.lower() == appliance_name.lower():
                if a.shed:
                    a.shed = False
                    a.on = True
                    return True
        return False

    def restore_all_loads(self):
        """Restore every shed appliance (called when ECG power returns)."""
        for a in self.appliances:
            a.shed = False
            a.on = True

    def shed_non_critical(self):
        """Shed all non-critical appliances in priority order."""
        shed_list = []
        for a in sorted(self.appliances, key=lambda x: x.priority, reverse=True):
            if not a.critical and not a.shed:
                a.shed = True
                a.on = False
                shed_list.append(a.name)
        return shed_list

    # -----------------------------------------------------------------------
    # Simulation control — used by simulation.py to inject events
    # -----------------------------------------------------------------------

    def trigger_outage(self, announced: bool = False):
        """Inject a power cut into the environment."""
        if not self.power_on:
            return  # already off
        self.power_on = False
        self.scheduled_cut = announced
        self.outage_count += 1
        self.current_outage = OutageRecord(
            start_time=self.sim_time,
            announced=announced
        )

    def restore_power(self):
        """Restore ECG power."""
        if self.power_on:
            return
        self.power_on = True
        self.scheduled_cut = False
        if self.current_outage:
            self.current_outage.close(self.sim_time)
            self.outage_history.append(self.current_outage)
            self.current_outage = None

    def advance_time(self, hours: float):
        """
        Advance simulated clock and consume fuel if generator is running.
        """
        self.sim_time += datetime.timedelta(hours=hours)
        if self.generator_running:
            fuel_used = self.burn_rate_Lph * hours
            fuel_used_pct = (fuel_used / self.fuel_capacity_L) * 100
            self.fuel_pct = max(0.0, self.fuel_pct - fuel_used_pct)
            self.fuel_consumed_L += fuel_used
            self.total_cost_GHS += fuel_used * self.fuel_price_GHS
            if self.fuel_pct <= 0:
                self.generator_running = False  # ran out of fuel

    def add_scheduled_cut(self, start_hour: int, end_hour: int):
        """Add an announced ECG outage window to the schedule."""
        self.ecg_schedule.append((start_hour, end_hour))

    def refuel(self, litres: float):
        """Add fuel to the tank."""
        added_pct = (litres / self.fuel_capacity_L) * 100
        self.fuel_pct = min(100.0, self.fuel_pct + added_pct)

    # -----------------------------------------------------------------------
    # Display helpers
    # -----------------------------------------------------------------------

    def print_appliances(self, title: str = "APPLIANCES"):
        """
        Print a compact appliance state table.
        Shows ON / SHED / OFF and why, with waking hours context.
        """
        hour = self.sim_time.hour
        waking = 6 <= hour < 23
        print(f"\n  {'─'*54}")
        print(f"  {title}   "
              f"[{'Waking hours' if waking else 'Night hours — generator withheld if off'}]"
              f"  {self.sim_time.strftime('%H:%M')}")
        print(f"  {'─'*54}")
        for a in self.appliances:
            if a.shed:
                state = "SHED ✂"
                reason = "non-critical — saved fuel"
            elif not a.on:
                state = "OFF  ✖"
                reason = "no power"
            else:
                state = "ON   ✓"
                reason = "CRITICAL — always on" if a.critical else "powered"
            crit_mark = " ★" if a.critical else "  "
            print(f"  {a.name:<22}{crit_mark}  {a.watt:>4}W  "
                  f"pri={a.priority}  {state}   {reason}")
        print(f"  {'─'*54}\n")

    def print_status(self):
        """Print a readable snapshot of the current environment state."""
        p = self.percepts
        time_str = self.sim_time.strftime("%H:%M")
        print("\n" + "="*60)
        print(f"  ENVIRONMENT STATUS  [{time_str}]")
        print("="*60)
        print(f"  ECG Power     : {'ON  ✓' if p['power_on'] else 'OFF ✗'}"
              f"{'  (scheduled)' if p['scheduled_cut'] else ''}")
        print(f"  Generator     : {'RUNNING ▶' if p['generator_running'] else 'stopped  ■'}")
        print(f"  Fuel level    : {p['fuel_pct']:>5.1f}%  "
              f"({p['fuel_litres']:.2f}L)  "
              f"~{p['fuel_runtime_hrs']:.1f}hrs runtime")
        print(f"  Fuel consumed : {p['fuel_consumed_L']:.3f}L  "
              f"(₵{p['total_cost_GHS']:.2f} total cost)")
        print(f"  Outages today : {p['outage_count']}")
        print(f"  Waking hours  : {'Yes' if p['is_waking_hours'] else 'No (night)'}")
        print("-"*60)
        print("  APPLIANCES:")
        for a in self.appliances:
            print(f"    {a}")
        print("="*60)