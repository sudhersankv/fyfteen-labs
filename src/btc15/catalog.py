"""Bot templates and plain-English decision labels."""

from __future__ import annotations

AGENT_KINDS = {
    "fair-value": {
        "title": "Fair Value",
        "summary": "Estimates P(YES) from how far Bitcoin is from the target, how much time is left, and recent volatility. Buys when that estimate is cheaper than the live Kalshi ask after fees.",
        "does": [
            "Uses distance from the strike, time remaining, and recent volatility.",
            "Compares model probability to the executable YES or NO ask.",
            "Trades across most of the 15-minute window when leftover edge is large enough.",
        ],
        "buy_when": [
            "Market data is fresh and a quote is executable.",
            "Estimated edge after fees clears the minimum-edge setting.",
            "Simple cash, spread, depth, and exposure checks pass.",
        ],
        "skip_when": ["Estimated edge is below the threshold.", "The book is stale or empty."],
        "entry_window_seconds": 840,
        "default_threshold": 0.03,
        "risk": "Moderate",
        "when": "Anytime leftover edge is large enough",
    },
    "momentum": {
        "title": "Momentum",
        "summary": "Starts from Fair Value, then tilts the estimate toward the recent Bitcoin move. Still one probability versus one executable ask.",
        "does": [
            "Uses the Fair Value estimate as a base.",
            "Adds short-term Bitcoin direction as a tilt, not a stack of vetoes.",
            "Does not require several time horizons to agree perfectly.",
        ],
        "buy_when": [
            "Fair Value plus recent momentum still leaves leftover edge after fees.",
            "Simple risk checks pass.",
        ],
        "skip_when": ["Edge is below the threshold.", "The book is stale or empty."],
        "entry_window_seconds": 840,
        "default_threshold": 0.025,
        "risk": "Moderate",
        "when": "When Bitcoin has been moving the same way for a few seconds",
    },
    "late-settlement": {
        "title": "Late Settlement",
        "summary": "Only looks for trades in the last 3 minutes, using Kalshi's official end-average mechanics. Quiet on purpose.",
        "does": [
            "Waits until the official 60-second BRTI average is becoming knowable.",
            "Uses settlement probability, not a last-tick guess.",
            "Usually makes fewer trades than the other templates.",
        ],
        "buy_when": [
            "Three minutes or less remain.",
            "Settlement probability versus the live ask leaves leftover edge after fees.",
        ],
        "skip_when": ["More than three minutes remain.", "Edge is below the threshold."],
        "entry_window_seconds": 180,
        "default_threshold": 0.025,
        "risk": "Lower",
        "when": "Last 3 minutes only",
    },
}

LEGACY_KINDS = {
    "fair-value": "fair-value",
    "momentum": "momentum",
    "late-settlement": "late-settlement",
    "settlement": "late-settlement",
    "trend-rider": "momentum",
    "hybrid": "fair-value",
    "trend": "momentum",
    "breakout": "momentum",
    "order-flow": "fair-value",
    "microstructure": "fair-value",
    "confirmation": "fair-value",
    "regime-confirmation": "fair-value",
    "consensus": "fair-value",
    "ensemble-consensus": "fair-value",
    "late-closer": "late-settlement",
    "direction": "momentum",
}

REASON_LABELS = {
    "stale": "Market data is stale",
    "invalid-target": "This market's target is invalid",
    "fees": "Production fees are not verified yet",
    "no-quote": "No executable quote",
    "window": "Outside the entry window",
    "below-edge": "Below minimum edge",
    "spread": "Spread too wide",
    "liquidity": "Not enough visible depth",
    "cooldown": "Cooling down after the last fill",
    "entries": "Already at max trades this contract",
    "exposure": "At the exposure cap",
    "cash": "Not enough fake cash",
    "paused": "Bot is paused",
    "buy": "Bought because leftover edge cleared the threshold",
    "sell": "Sold because the bid was better than holding",
    "hold-position": "Holding an open paper position",
}

EDGE_FORMULA = "edge = model probability − executable ask − fee − safety margin"

KNOBS = [
    {"id": "budget", "label": "Fake balance", "control": True, "beginner": True,
     "meaning": "Simulated cash assigned to this bot. New buys size from working cash."},
    {"id": "threshold", "label": "Minimum edge", "control": True, "beginner": True,
     "meaning": "How cheap the contract must be versus the model after fees. 4% means about 4 cents."},
    {"id": "max_order_dollars", "label": "Max dollars per trade", "control": True, "beginner": True,
     "meaning": "Hard cap on one simulated buy."},
    {"id": "max_market_exposure", "label": "Maximum exposure", "control": True, "beginner": True,
     "meaning": "Maximum entry cost this bot may commit to one contract."},
    {"id": "deploy", "label": "Deploy / Pause", "control": True, "beginner": True,
     "meaning": "Pause stops new buys. Open positions still mark to market and settle."},
    {"id": "retire", "label": "Retire", "control": True, "beginner": True,
     "meaning": "Removes the bot after it is flat. History stays."},
]

SHARED_TRIGGERS = {
    "buy": [
        "Bot is deployed and the book is fresh.",
        "A YES or NO ask is executable.",
        "model probability − ask − fee − safety ≥ minimum edge.",
        "Spread, depth, cash, cooldown, and exposure checks pass.",
        "Late Settlement also requires three minutes or less remaining.",
    ],
    "sell": [
        "The executable bid is richer than the modeled hold value by the exit margin.",
        "Or the model has clearly reversed against an open position.",
    ],
    "blocked": [
        "stale or empty book",
        "below minimum edge",
        "spread, depth, cooldown, trade-count, or exposure caps",
    ],
}


def leftover_edge(probability: float | None, ask: float | None, fee: float,
                  safety: float, uncertainty: float = 0.0) -> float | None:
    if probability is None or ask is None:
        return None
    return probability - ask - fee - safety - uncertainty


def classify_opportunities(*, seconds_left: float, fresh: bool,
                           yes_ask: float | None, no_ask: float | None,
                           p_settlement: float | None, p_trend: float | None,
                           fee_yes: float, fee_no: float,
                           safety: float, uncertainty: float,
                           p_terminal: float | None = None) -> list[dict]:
    forecasts = {
        "fair-value": p_terminal if p_terminal is not None else p_settlement,
        "momentum": p_trend,
        "late-settlement": p_settlement,
    }
    classified = []
    for kind, meta in AGENT_KINDS.items():
        probability = forecasts[kind]
        window_open = seconds_left <= meta["entry_window_seconds"]
        yes_edge = leftover_edge(probability, yes_ask, fee_yes, safety, 0)
        no_edge = leftover_edge(None if probability is None else 1 - probability,
                               no_ask, fee_no, safety, 0)
        open_choices = [(edge, side, price) for edge, side, price in
                        ((yes_edge, "yes", yes_ask), (no_edge, "no", no_ask)) if edge is not None]
        edge = side = price = None
        if open_choices:
            edge, side, price = max(open_choices)
        if not fresh:
            status, plain = "stale", "Feed is stale."
        elif not window_open:
            status, plain = "closed", f"Window closed. {meta['when']}."
        elif probability is None:
            status, plain = "warming", "Forecast is still warming up."
        elif edge is None:
            status, plain = "no-book", "No executable ask."
        elif edge > 0:
            status, plain = "open", f"Open leftover on {(side or '').upper()}."
        else:
            status, plain = "no-edge", "The ask is not cheap after fees."
        classified.append({
            "id": kind, "title": meta["title"], "status": status, "plain_status": plain,
            "side": side if status == "open" else None, "edge": edge, "price": price,
            "window_open": window_open, "fresh": fresh, "window": meta["when"],
            "window_seconds": meta["entry_window_seconds"],
        })
    return classified


def kind_guide(kind: str) -> dict:
    resolved = LEGACY_KINDS.get(kind, kind)
    guide = dict(AGENT_KINDS.get(resolved, {
        "title": kind, "summary": "Custom template.", "does": [], "buy_when": [],
        "skip_when": [], "default_threshold": 0.03, "risk": "Moderate", "when": "",
    }))
    guide["kind"] = resolved
    return guide


# Back-compat names used by older tests and comments.
OPPORTUNITY_DEFINITION = (
    "An opportunity is leftover executable edge: a live YES or NO ask that is "
    "still cheap after fees and a safety margin versus the template's probability."
)
OPPORTUNITY_FORMULA = EDGE_FORMULA
OPPORTUNITY_BUCKETS = [
    {"id": kind, "title": meta["title"], "plain_name": meta["title"],
     "window": meta["when"], "window_seconds": meta["entry_window_seconds"],
     "acted_on_by": [kind], "template_ids": [kind]}
    for kind, meta in AGENT_KINDS.items()
]
NOT_OPPORTUNITIES = [
    {"title": "A raw YES/NO guess",
     "why": "Believing Bitcoin finishes above the strike is a view, not a trade."},
    {"title": "The last print or the midpoint",
     "why": "Paper fills use the visible ask, never the midpoint."},
]
