AGENT_KINDS = {
    "settlement": {
        "title": "Late closer",
        "plain_job": "Late closer",
        "summary": "Buys only in the last 3 minutes, when Kalshi's official end-average is almost known. Quiet for the rest of the window.",
        "does": [
            "Uses the zero-drift settlement probability, not a last-price guess.",
            "During the final minute it incorporates each observed official BRTI reading.",
            "Trades rarely and may hold a strong late position through settlement.",
        ],
        "buy_when": [
            "Three minutes or less remain.",
            "Executable net edge survives fees, spread, uncertainty, and safety margin.",
            "The preferred side persists and all book/risk checks pass.",
        ],
        "skip_when": ["More than three minutes remain.", "The settlement estimate is not informative enough."],
        "entry_window_seconds": 180,
        "opportunity_bucket": "settlement",
        "default_threshold": 0.03,
    },
    "trend-rider": {
        "title": "Direction",
        "plain_job": "Direction",
        "summary": "Buys in the last 6 minutes when Bitcoin has been moving the same way across 5, 30, and 60 seconds. This is the job that actually trades most often.",
        "does": [
            "Requires at least two momentum horizons to agree.",
            "Uses a heavily shrunk drift forecast.",
            "Uses order flow only to avoid badly timed entries, not as a 15-minute forecast.",
        ],
        "buy_when": [
            "Six minutes or less remain and a non-choppy trend persists.",
            "Trend direction agrees with the chosen YES/NO side.",
            "Executable net edge clears the configured threshold.",
        ],
        "skip_when": ["Momentum horizons conflict.", "Strike crossings indicate chop.", "Strong immediate flow opposes the trade."],
        "entry_window_seconds": 360,
        "opportunity_bucket": "trend",
        "default_threshold": 0.035,
    },
    "hybrid": {
        "title": "Both agree",
        "plain_job": "Both agree",
        "summary": "Buys in the last 4 minutes only when the late-average call and the direction call pick the same side.",
        "does": [
            "Blends settlement and trend forecasts instead of stacking many indicators.",
            "Requires both forecasts to choose the same side.",
            "Rejects strong opposing short-lived order flow.",
        ],
        "buy_when": [
            "Four minutes or less remain.",
            "Settlement and trend forecasts agree and recent choices persist.",
            "Executable net edge clears all costs and the configured threshold.",
        ],
        "skip_when": ["Forecasts disagree.", "The preferred side is unstable.", "Immediate flow is strongly adverse."],
        "entry_window_seconds": 240,
        "opportunity_bucket": "hybrid",
        "default_threshold": 0.03,
    },
}

OPPORTUNITY_DEFINITION = (
    "An opportunity is leftover executable edge: a live YES or NO ask that is "
    "still cheap after you subtract the fee, safety margin, and model uncertainty "
    "from the bucket's forecast. Agents are configured to act on one named bucket. "
    "They wait when that bucket is closed. Waiting is not a −100% loss."
)

OPPORTUNITY_FORMULA = "leftover = forecast − executable ask − fee − safety − uncertainty"

NOT_OPPORTUNITIES = [
    {"title": "A raw YES/NO guess",
     "why": "Believing Bitcoin will finish above the strike is a view, not a trade. The trade exists only if the ask is cheaper than that view after costs."},
    {"title": "The last Kalshi print or the midpoint",
     "why": "You cannot fill at the last trade or between bid and ask. Only the visible ask is executable."},
    {"title": "The first 9–12 minutes of the window",
     "why": "This contract settles on a 60-second average at the end. Early prices are mostly a coin flip after fees. Our recorded data was weaker than the market itself beyond six minutes."},
    {"title": "A one-second order-book imbalance",
     "why": "Book pressure can time a fill for a few seconds. Research on BTC order-book imbalance shows that signal decaying in tens of seconds, not over a 15-minute contract."},
    {"title": "News, chat, or an LLM hunch",
     "why": "Those are not a priced leftover versus the live ask. FYFTEN only assigns one of these three jobs; it does not invent a forecast."},
]

OPPORTUNITY_BUCKETS = [
    {
        "id": "settlement",
        "title": "Settlement misprice",
        "plain_name": "Settlement misprice",
        "forecast": "Official 60 one-second BRTI readings in the final minute, averaged",
        "window": "Last 3 minutes",
        "window_seconds": 180,
        "acted_on_by": ["settlement"],
        "template_ids": ["settlement-careful"],
        "how_it_acts": "Buys the cheap late side and prefers to hold a strong thesis through official settlement.",
        "not": "Does not chase the first 12 minutes of direction.",
        "teach": "Kalshi does not settle this contract on the last Bitcoin tick. YES wins if the official average of 60 BRTI prints in the final minute is at or above the target. Late in the window that average becomes knowable, so a cheap ask can be a real leftover.",
        "example": "90 seconds left, 40 official prints already average above the target, and NO is still offered at 40¢. That leftover is a settlement misprice if it survives fees.",
        "user_can_change": "You can make this bot pickier (higher leftover required, later entry) or slightly earlier. You cannot turn it into an all-day trend bot without changing its bucket.",
        "source": "Kalshi crypto settlement rules: 60 CF Benchmarks RTI readings, one per second, in the expiration minute.",
    },
    {
        "id": "trend",
        "title": "Confirmed short trend",
        "plain_name": "Confirmed short trend",
        "forecast": "BRTI direction that agrees across 5, 30, and 60 seconds, projected only a little",
        "window": "Last 6 minutes",
        "window_seconds": 360,
        "acted_on_by": ["trend-rider"],
        "template_ids": ["trend-confirmed"],
        "how_it_acts": "Enters only when at least two horizons agree, then uses the book to avoid a badly timed fill.",
        "not": "Does not treat a one-second book imbalance as a 15-minute forecast.",
        "teach": "Short Bitcoin moves contain some information for seconds to a few minutes, then fade. This bucket asks whether a persistent move has not yet been fully paid in the Kalshi ask. It never extrapolates the last tick at full strength.",
        "example": "Four minutes left, 5s / 30s / 60s BRTI all up, and YES is still cheaper than that shrunk forecast after fees.",
        "user_can_change": "You can require a larger leftover or a later start. Making the window much earlier usually means paying a coin-flip price.",
        "source": "BTC order-book and return studies: imbalance and flow help for seconds; minute-scale direction is a different, weaker claim.",
    },
    {
        "id": "hybrid",
        "title": "Agreement",
        "plain_name": "Agreement",
        "forecast": "Settlement and short-trend forecasts choosing the same YES or NO",
        "window": "Last 4 minutes",
        "window_seconds": 240,
        "acted_on_by": ["hybrid"],
        "template_ids": ["hybrid-balanced"],
        "how_it_acts": "Trades only the intersection: both forecasts agree and leftover survives costs.",
        "not": "Does not fire when the two forecasts disagree, even if one looks cheap.",
        "teach": "This is the pickiest bucket. It exists because one model can look cheap while the other says you are early or on the wrong side. Agreement means both named forecasts want the same contract.",
        "example": "Settlement says YES leftover is +6¢ and the short trend also says YES. Only then does an Agreement bot consider a buy.",
        "user_can_change": "Raising leftover makes it rarer. Lowering leftover makes it busier and more likely to pay noise.",
        "source": "Prediction-market execution practice: theoretical edge is not tradable until forecasts agree enough to survive spread, fees, and timing.",
    },
]


def leftover_edge(probability: float | None, ask: float | None, fee: float,
                  safety: float, uncertainty: float) -> float | None:
    if probability is None or ask is None:
        return None
    return probability - ask - fee - safety - uncertainty


def classify_opportunities(*, seconds_left: float, fresh: bool,
                           yes_ask: float | None, no_ask: float | None,
                           p_settlement: float | None, p_trend: float | None,
                           fee_yes: float, fee_no: float,
                           safety: float, uncertainty: float) -> list[dict]:
    forecasts = {
        "settlement": p_settlement,
        "trend": p_trend,
        "hybrid": (None if p_settlement is None or p_trend is None else
                   (p_settlement + p_trend) / 2 if (p_settlement - .5) * (p_trend - .5) > 0 else None),
    }
    classified = []
    for bucket in OPPORTUNITY_BUCKETS:
        probability = forecasts[bucket["id"]]
        window_open = seconds_left <= bucket["window_seconds"]
        yes_edge = leftover_edge(probability, yes_ask, fee_yes, safety, uncertainty)
        no_edge = leftover_edge(None if probability is None else 1 - probability,
                               no_ask, fee_no, safety, uncertainty)
        choices = [(yes_edge, "yes", yes_ask), (no_edge, "no", no_ask)]
        open_choices = [row for row in choices if row[0] is not None]
        side, edge, price = (None, None, None)
        if open_choices:
            edge, side, price = max(open_choices)
        status = "waiting"
        if not fresh:
            status = "stale"
        elif not window_open:
            status = "closed"
        elif probability is None:
            status = "disagreement" if bucket["id"] == "hybrid" else "warming"
        elif edge is None:
            status = "no-book"
        elif edge > 0:
            status = "open"
        else:
            status = "no-edge"
        plain = {
            "stale": "Feed is stale — not an opportunity until the book and BRTI agree.",
            "closed": f"Window closed. This bucket only opens in the {bucket['window'].lower()}.",
            "disagreement": "Settlement and trend disagree, so Agreement is not open.",
            "warming": "Forecast is still warming up.",
            "no-book": "No executable ask on the cheap side.",
            "open": f"Open leftover on {(side or '').upper()}.",
            "no-edge": "Forecast exists, but after costs the ask is not cheap.",
            "waiting": "Waiting for a readable market.",
        }[status]
        classified.append({
            **bucket, "status": status, "plain_status": plain,
            "side": side if status == "open" else None,
            "edge": edge, "price": price, "window_open": window_open, "fresh": fresh,
        })
    return classified

KNOBS = [
    {"id": "budget", "label": "Budget", "control": True,
     "meaning": "Paper cash assigned to this agent. New buys size from working cash, not banked profit."},
    {"id": "threshold", "label": "Min edge", "control": True,
     "meaning": "How cheap the contract must be versus the agent's probability after fees, safety margin, and uncertainty. 3% means about 3 cents of leftover edge."},
    {"id": "deploy", "label": "Deploy / Pause", "control": True,
     "meaning": "Pause stops new buys. Open positions still mark to market and settle officially."},
    {"id": "retire", "label": "Retire", "control": True,
     "meaning": "Removes the agent from the desk after it is flat. Historical trades remain."},
    {"id": "safety_margin", "label": "Safety margin", "control": False,
     "meaning": "Extra haircut so a quote that is only barely fair is not bought."},
    {"id": "confidence", "label": "Confidence", "control": False,
     "meaning": "Rises as ~60 seconds of BRTI history accumulates and models agree. Low confidence blocks autonomous buys and shrinks size."},
    {"id": "uncertainty", "label": "Uncertainty", "control": False,
     "meaning": "Penalty subtracted from edge when settlement and terminal models disagree or data is thin."},
    {"id": "size", "label": "Next size", "control": False,
     "meaning": "Confidence-scaled fractional Kelly, capped at a fraction of cash and the hard dollars-per-trade limit."},
]

SHARED_TRIGGERS = {
    "buy": [
        "Agent is deployed and the executable book is fresh.",
        "Model confidence is high enough (about one minute of synchronized BRTI).",
        "Specialist filter for this kind passes.",
        "probability − ask − fee − safety margin − uncertainty ≥ min edge.",
        "Spread, liquidity, time remaining, cooldown, trade-count, and exposure checks pass.",
    ],
    "sell": [
        "The executable bid is richer than the current hold value.",
        "Or a persistent forecast reversal breaks the original thesis.",
        "Or an open profit is reduced after probability and order flow weaken together.",
        "Order flow alone never forces a loss. Strong late winners may settle.",
    ],
    "blocked": [
        "stale or unsynchronized feed",
        "insufficient model history / confidence",
        "too near close, wide spread, or thin depth",
        "cooldown, max trades per market, or exposure caps",
    ],
}


def kind_guide(kind: str) -> dict:
    guide = dict(AGENT_KINDS.get(kind, {
        "title": kind, "summary": "Custom specialist.", "does": [], "buy_when": [],
        "skip_when": [], "default_threshold": 0.03,
    }))
    guide["kind"] = kind
    return guide
