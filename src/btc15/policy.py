from __future__ import annotations

from typing import Any


POLICY_FIELDS = [
    {"id":"min_confidence","label":"Minimum confidence","unit":"percent","min":.10,"max":.95,"step":.05,
     "beginner":True,"meaning":"Blocks new entries until enough synchronized BRTI history exists and the probability models agree."},
    {"id":"uncertainty_penalty","label":"Uncertainty penalty","unit":"multiple","min":0,"max":2,"step":.1,
     "beginner":False,"meaning":"Multiplies model uncertainty before subtracting it from executable edge. Higher is more skeptical."},
    {"id":"max_spread","label":"Maximum spread","unit":"percent","min":.005,"max":.20,"step":.005,
     "beginner":True,"meaning":"Refuses expensive books when executable ask minus bid exceeds this amount."},
    {"id":"min_liquidity","label":"Minimum visible depth","unit":"contracts","min":.1,"max":500,"step":1,
     "beginner":True,"meaning":"Requires this many visible contracts on the executable side before entering."},
    {"id":"entry_start_seconds","label":"Earliest entry","unit":"seconds left","min":60,"max":900,"step":15,
     "beginner":False,"meaning":"The bot waits until the countdown falls below this value, avoiding very early low-information entries."},
    {"id":"entry_stop_seconds","label":"Latest entry","unit":"seconds left","min":5,"max":300,"step":5,
     "beginner":True,"meaning":"No new entries after the countdown falls below this value. Existing positions may still exit or settle."},
    {"id":"cooldown_seconds","label":"Cooldown","unit":"seconds","min":0,"max":300,"step":5,
     "beginner":True,"meaning":"Minimum wait after a fill before the agent can enter this market again."},
    {"id":"max_entries_per_market","label":"Maximum entries","unit":"count","min":1,"max":20,"step":1,
     "beginner":True,"meaning":"Hard cap on buy fills in one 15-minute contract, including re-entries."},
    {"id":"max_market_exposure","label":"Market exposure cap","unit":"dollars","min":1,"max":10000,"step":1,
     "beginner":True,"meaning":"Maximum entry cost this agent may have committed to one contract."},
    {"id":"max_total_exposure","label":"Total exposure cap","unit":"dollars","min":1,"max":10000,"step":1,
     "beginner":False,"meaning":"Maximum entry cost across every currently open contract for this agent."},
    {"id":"capital_fraction","label":"Cash fraction cap","unit":"percent","min":.01,"max":1,"step":.05,
     "beginner":True,"meaning":"Maximum fraction of working cash considered for one order before other sizing limits."},
    {"id":"max_order_dollars","label":"Maximum order","unit":"dollars","min":1,"max":10000,"step":1,
     "beginner":True,"meaning":"Hard dollar cap on one simulated buy, independent of Kelly sizing."},
    {"id":"fractional_kelly","label":"Kelly fraction","unit":"multiple","min":.05,"max":.50,"step":.05,
     "beginner":False,"meaning":"Scales Kelly sizing. Quarter-Kelly (0.25) is conservative; higher values amplify calibration errors and drawdowns."},
    {"id":"exit_edge","label":"Exit value margin","unit":"percent","min":0,"max":.10,"step":.005,
     "beginner":False,"meaning":"Sells when the executable bid exceeds modeled hold value by this margin, before reversal and adverse-flow exits."},
]


def defaults(settings: Any) -> dict[str,float]:
    return {
        "min_confidence":settings.model_min_confidence,
        "uncertainty_penalty":settings.model_uncertainty_penalty,
        "max_spread":settings.max_spread,
        "min_liquidity":settings.min_liquidity,
        "entry_start_seconds":600.0,
        "entry_stop_seconds":settings.min_seconds_remaining,
        "cooldown_seconds":max(60.0,float(settings.cooldown_seconds)),
        "max_entries_per_market":3.0,
        "max_market_exposure":settings.max_market_exposure,
        "max_total_exposure":settings.max_total_exposure,
        "capital_fraction":min(.15,float(settings.capital_fraction_per_trade)),
        "max_order_dollars":min(5.0,float(settings.dollars_per_trade)),
        "fractional_kelly":min(.20,float(settings.fractional_kelly)),
        "exit_edge":.015,
    }


def presets(settings: Any) -> dict[str,dict[str,float]]:
    base=defaults(settings)
    return {
        "careful":{**base,"min_confidence":.40,"uncertainty_penalty":1.0,
                   "max_spread":.06,"min_liquidity":5,"entry_start_seconds":360,
                   "entry_stop_seconds":20,"cooldown_seconds":45,
                   "max_entries_per_market":2,"capital_fraction":.10,
                   "max_order_dollars":4,"fractional_kelly":.15,"exit_edge":.02},
        "balanced":base,
        "experimental":{**base,"min_confidence":.35,"uncertainty_penalty":.75,
                        "max_spread":.08,"min_liquidity":3,"entry_start_seconds":600,
                        "entry_stop_seconds":15,"cooldown_seconds":30,
                        "max_entries_per_market":4,"capital_fraction":.20,
                        "max_order_dollars":10,"fractional_kelly":.25,"exit_edge":.01},
    }


def bot_templates(settings: Any) -> list[dict[str,Any]]:
    available=presets(settings)
    return [
        {
            "id":"settlement-careful","title":"Late closer",
            "tagline":"Last 3 minutes only",
            "kind":"settlement","risk":"Lower","cadence":"Usually 0–2 entries per market",
            "recommended_for":"When you want a quiet bot that only acts near the official end-average.",
            "threshold":.025,
            "policy":{**available["careful"],"entry_start_seconds":180,
                      "entry_stop_seconds":20,"max_entries_per_market":2},
            "bucket":"settlement",
            "why":"Waits until the official final-minute average becomes much easier to estimate.",
        },
        {
            "id":"trend-confirmed","title":"Direction",
            "tagline":"Last 6 minutes · trades the most",
            "kind":"trend-rider","risk":"Moderate","cadence":"Usually 0–3 entries per market",
            "recommended":True,
            "recommended_for":"The default job if you want the bot to actually look for buys.",
            "threshold":.03,
            "policy":{**available["balanced"],"entry_start_seconds":360,
                      "entry_stop_seconds":30,"max_entries_per_market":3},
            "bucket":"trend",
            "why":"Requires 5-, 30-, and 60-second evidence to align before considering a trade.",
        },
        {
            "id":"hybrid-balanced","title":"Both agree",
            "tagline":"Last 4 minutes · pickiest",
            "kind":"hybrid","risk":"Moderate","cadence":"Usually 0–3 entries per market",
            "recommended_for":"When you want a buy only if late-average and direction agree.",
            "threshold":.025,
            "policy":{**available["balanced"],"entry_start_seconds":240,
                      "entry_stop_seconds":20,"max_entries_per_market":3},
            "bucket":"hybrid",
            "why":"Trades only when settlement and trend forecasts choose the same side.",
        },
    ]


def normalize(candidate: dict | None, settings: Any) -> dict[str,float]:
    values=defaults(settings)
    if candidate:
        allowed={field["id"]:field for field in POLICY_FIELDS}
        for key,value in candidate.items():
            if key not in allowed:
                continue
            field=allowed[key]
            number=float(value)
            if not field["min"] <= number <= field["max"]:
                raise ValueError(f"{field['label']} must be between {field['min']} and {field['max']}")
            values[key]=number
    if values["entry_stop_seconds"] >= values["entry_start_seconds"]:
        raise ValueError("Latest entry must be below earliest entry")
    if values["max_market_exposure"] > values["max_total_exposure"]:
        raise ValueError("Market exposure cannot exceed total exposure")
    values["max_entries_per_market"]=int(values["max_entries_per_market"])
    return values
