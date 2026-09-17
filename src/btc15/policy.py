from __future__ import annotations

from typing import Any


BEGINNER_FIELDS = [
    {"id": "max_order_dollars", "label": "Max dollars per trade", "unit": "dollars",
     "min": 1, "max": 10000, "step": 1, "beginner": True,
     "meaning": "Hard cap on one simulated buy."},
    {"id": "max_market_exposure", "label": "Maximum exposure", "unit": "dollars",
     "min": 1, "max": 10000, "step": 1, "beginner": True,
     "meaning": "Maximum entry cost this bot may have in one contract."},
]

ADVANCED_FIELDS = [
    {"id": "entry_start_seconds", "label": "Earliest entry", "unit": "seconds left",
     "min": 60, "max": 900, "step": 15, "beginner": False,
     "meaning": "The bot waits until this many seconds remain before it can buy."},
    {"id": "entry_stop_seconds", "label": "Latest entry", "unit": "seconds left",
     "min": 5, "max": 300, "step": 5, "beginner": False,
     "meaning": "No new buys after the countdown falls below this. Open positions may still settle."},
    {"id": "max_spread", "label": "Maximum spread", "unit": "percent",
     "min": .005, "max": .20, "step": .005, "beginner": False,
     "meaning": "Skip the book when ask minus bid is wider than this."},
    {"id": "min_liquidity", "label": "Minimum visible depth", "unit": "contracts",
     "min": .1, "max": 500, "step": 1, "beginner": False,
     "meaning": "Need this many visible contracts on the executable side."},
    {"id": "cooldown_seconds", "label": "Cooldown", "unit": "seconds",
     "min": 0, "max": 300, "step": 5, "beginner": False,
     "meaning": "Minimum wait after a fill before another buy in this market."},
    {"id": "max_entries_per_market", "label": "Maximum entries", "unit": "count",
     "min": 1, "max": 20, "step": 1, "beginner": False,
     "meaning": "Cap on buy fills in one 15-minute contract."},
    {"id": "max_total_exposure", "label": "Total exposure cap", "unit": "dollars",
     "min": 1, "max": 10000, "step": 1, "beginner": False,
     "meaning": "Maximum entry cost across every open contract for this bot."},
    {"id": "capital_fraction", "label": "Cash fraction cap", "unit": "percent",
     "min": .01, "max": 1, "step": .05, "beginner": False,
     "meaning": "Maximum fraction of working cash considered for one order."},
    {"id": "exit_edge", "label": "Exit value margin", "unit": "percent",
     "min": 0, "max": .10, "step": .005, "beginner": False,
     "meaning": "Sell when the executable bid exceeds modeled hold value by this amount."},
]

POLICY_FIELDS = BEGINNER_FIELDS + ADVANCED_FIELDS


def defaults(settings: Any) -> dict[str, float]:
    return {
        "max_spread": settings.max_spread,
        "min_liquidity": settings.min_liquidity,
        "entry_start_seconds": 840.0,
        "entry_stop_seconds": settings.min_seconds_remaining,
        "cooldown_seconds": max(20.0, float(settings.cooldown_seconds)),
        "max_entries_per_market": 3.0,
        "max_market_exposure": settings.max_market_exposure,
        "max_total_exposure": settings.max_total_exposure,
        "capital_fraction": min(.25, float(settings.capital_fraction_per_trade)),
        "max_order_dollars": min(20.0, float(settings.dollars_per_trade)),
        "exit_edge": .02,
    }


def presets(settings: Any) -> dict[str, dict[str, float]]:
    base = defaults(settings)
    return {
        "fair-value": {**base, "entry_start_seconds": 840, "max_order_dollars": 10,
                       "cooldown_seconds": 20, "max_entries_per_market": 3},
        "momentum": {**base, "entry_start_seconds": 840, "max_order_dollars": 10,
                     "cooldown_seconds": 15, "max_entries_per_market": 4},
        "late-settlement": {**base, "entry_start_seconds": 180, "max_order_dollars": 8,
                            "cooldown_seconds": 30, "max_entries_per_market": 2,
                            "entry_stop_seconds": 15},
    }


def bot_templates(settings: Any) -> list[dict[str, Any]]:
    available = presets(settings)
    budget = float(settings.bankroll)
    return [
        {
            "id": "fair-value", "title": "Fair Value",
            "tagline": "Distance, time, and volatility versus the live ask",
            "kind": "fair-value", "risk": "Moderate",
            "when": "Most of the 15-minute window",
            "recommended": True,
            "recommended_for": "The default. Easiest to explain.",
            "threshold": .03, "default_budget": budget,
            "policy": available["fair-value"],
            "why": "Estimates P(YES) from how far Bitcoin is from the target, how much time is left, and recent volatility. Buys when that is cheaper than the Kalshi ask after fees.",
        },
        {
            "id": "momentum", "title": "Momentum",
            "tagline": "Fair Value plus a short Bitcoin tilt",
            "kind": "momentum", "risk": "Moderate",
            "when": "When Bitcoin has been moving the same way",
            "recommended_for": "When you want a directional tilt without extra vetoes.",
            "threshold": .025, "default_budget": budget,
            "policy": available["momentum"],
            "why": "Same leftover-edge rule as Fair Value, with recent Bitcoin direction mixed into the probability.",
        },
        {
            "id": "late-settlement", "title": "Late Settlement",
            "tagline": "Last 3 minutes · official average",
            "kind": "late-settlement", "risk": "Lower",
            "when": "Last 3 minutes only",
            "recommended_for": "A quiet bot that demonstrates settlement mechanics.",
            "threshold": .025, "default_budget": budget,
            "policy": available["late-settlement"],
            "why": "Waits until Kalshi's official 60-second BRTI average is almost known, then looks for leftover edge.",
        },
    ]


def normalize(candidate: dict | None, settings: Any) -> dict[str, float]:
    values = defaults(settings)
    if candidate:
        allowed = {field["id"]: field for field in POLICY_FIELDS}
        # Ignore retired keys from older bots instead of crashing restore.
        for key, value in candidate.items():
            if key not in allowed:
                continue
            field = allowed[key]
            number = float(value)
            if not field["min"] <= number <= field["max"]:
                raise ValueError(f"{field['label']} must be between {field['min']} and {field['max']}")
            values[key] = number
    if values["entry_stop_seconds"] >= values["entry_start_seconds"]:
        raise ValueError("Latest entry must be below earliest entry")
    if values["max_market_exposure"] > values["max_total_exposure"]:
        raise ValueError("Market exposure cannot exceed total exposure")
    values["max_entries_per_market"] = int(values["max_entries_per_market"])
    return values
