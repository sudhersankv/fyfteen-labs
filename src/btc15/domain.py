from __future__ import annotations

import math
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def iso_ts(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value) / (1000 if value > 10_000_000_000 else 1)
    if not value:
        return 0
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


@dataclass
class Market:
    ticker: str
    event_ticker: str
    target: float
    open_ts: float
    close_ts: float
    condition: str
    status: str = ""
    result: str | None = None
    expiration_value: float | None = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_api(cls, m: dict) -> "Market":
        target = next((f(m.get(k)) for k in ("floor_strike", "strike_value", "cap_strike")
                       if m.get(k) not in (None, "")), 0.0)
        if not target:
            text = " ".join(str(m.get(k, "")) for k in ("title", "subtitle", "yes_sub_title"))
            numbers = re.findall(r"\$?([\d,]+(?:\.\d+)?)", text)
            candidates = [f(value.replace(",","")) for value in numbers]
            target = next((value for value in reversed(candidates) if value >= 1000),0.0)
        return cls(
            ticker=m["ticker"], event_ticker=m.get("event_ticker", ""),
            target=target, open_ts=iso_ts(m.get("open_time")),
            close_ts=iso_ts(m.get("close_time") or m.get("expected_expiration_time")),
            condition=m.get("rules_primary") or m.get("yes_sub_title") or m.get("title", ""),
            status=m.get("status", ""), result=m.get("result") or None,
            expiration_value=f(m["expiration_value"]) if m.get("expiration_value") else None,
            raw=m,
        )


class OrderBook:
    """Unified YES-price book: yes levels are bids; no levels are YES asks."""
    def __init__(self) -> None:
        self.yes: dict[float, float] = {}
        self.no: dict[float, float] = {}
        self.seq = 0
        self.valid = False
        self.updated = 0.0

    def snapshot(self, msg: dict, seq: int = 0) -> None:
        self.yes = {f(p): f(q) for p, q in msg.get("yes_dollars_fp", msg.get("yes", []))}
        self.no = {f(p): f(q) for p, q in msg.get("no_dollars_fp", msg.get("no", []))}
        self.seq, self.valid, self.updated = seq, True, time.time()

    def delta(self, msg: dict, seq: int) -> bool:
        if not self.valid:
            return False
        levels = self.yes if msg["side"] == "yes" else self.no
        price, qty = f(msg.get("price_dollars", msg.get("price"))), f(msg.get("delta_fp", msg.get("delta")))
        levels[price] = max(0, levels.get(price, 0) + qty)
        if levels[price] <= 1e-9:
            levels.pop(price, None)
        self.seq, self.updated = seq, time.time()
        return True

    def invalidate(self) -> None:
        self.valid = False

    @property
    def yes_bid(self) -> float | None:
        return max(self.yes, default=None)

    @property
    def yes_ask(self) -> float | None:
        return min(self.no, default=None)

    @property
    def no_bid(self) -> float | None:
        return 1 - self.yes_ask if self.yes_ask is not None else None

    @property
    def no_ask(self) -> float | None:
        return 1 - self.yes_bid if self.yes_bid is not None else None

    def walk_buy(self, side: str, contracts: float) -> tuple[float, float]:
        filled,cost,_=self.walk_buy_details(side,contracts)
        return filled,cost

    def walk_buy_details(self,side: str,contracts: float) -> tuple[float,float,list[tuple[float,float]]]:
        if side == "yes":
            levels = sorted(self.no.items())
            convert = lambda p: p
        else:
            levels = sorted(self.yes.items(), reverse=True)
            convert = lambda p: 1 - p
        remaining,cost,filled,legs=contracts,0.0,0.0,[]
        for p, q in levels:
            take = min(q, remaining)
            execution_price=convert(p)
            cost += execution_price*take
            filled += take
            legs.append((execution_price,take))
            remaining -= take
            if remaining <= 1e-9:
                break
        return filled,cost,legs

    def walk_sell(self, side: str, contracts: float) -> tuple[float, float]:
        filled,proceeds,_=self.walk_sell_details(side,contracts)
        return filled,proceeds

    def walk_sell_details(self,side: str,contracts: float) -> tuple[float,float,list[tuple[float,float]]]:
        if side == "yes":
            levels = sorted(self.yes.items(), reverse=True)
            convert = lambda p: p
        else:
            levels = sorted(self.no.items())
            convert = lambda p: 1 - p
        remaining,proceeds,filled,legs=contracts,0.0,0.0,[]
        for p, q in levels:
            take = min(q, remaining)
            execution_price=convert(p)
            proceeds += execution_price*take
            filled += take
            legs.append((execution_price,take))
            remaining -= take
            if remaining <= 1e-9:
                break
        return filled,proceeds,legs

    def depth(self, side: str, levels: int = 5) -> float:
        data = self.no if side == "ask" else self.yes
        ordered = sorted(data, reverse=side != "ask")
        return sum(data[p] for p in ordered[:levels])

    @property
    def imbalance(self) -> float:
        bid, ask = self.depth("bid"), self.depth("ask")
        return (bid - ask) / (bid + ask) if bid + ask else 0.0

    @property
    def weighted_imbalance(self) -> float:
        bids = sorted(self.yes.items(),reverse=True)[:5]
        asks = sorted(self.no.items())[:5]
        bid_depth = sum(q/(i+1) for i,(_,q) in enumerate(bids))
        ask_depth = sum(q/(i+1) for i,(_,q) in enumerate(asks))
        return (bid_depth-ask_depth)/(bid_depth+ask_depth) if bid_depth+ask_depth else 0.0

    @property
    def microprice(self) -> float | None:
        bid,ask = self.yes_bid,self.yes_ask
        if bid is None or ask is None:
            return None
        bid_size,ask_size = self.yes[bid],self.no[ask]
        return ((ask*bid_size+bid*ask_size)/(bid_size+ask_size)
                if bid_size+ask_size else (bid+ask)/2)


class PriceHistory:
    def __init__(self, maxlen: int = 10_000) -> None:
        self.values: deque[tuple[float, float]] = deque(maxlen=maxlen)

    def add(self, ts: float, value: float) -> None:
        self.values.append((ts, value))

    def at(self, seconds_ago: float, now: float) -> float | None:
        target = now - seconds_ago
        return next((v for t, v in reversed(self.values) if t <= target), None)

    def ret(self, seconds: float, now: float) -> float:
        if not self.values:
            return 0.0
        old = self.at(seconds, now)
        return (self.values[-1][1] / old - 1) if old else 0.0

    def volatility(self, seconds: float = 60) -> float:
        cutoff = self.values[-1][0] - seconds if self.values else 0
        vals = [v for t, v in self.values if t >= cutoff]
        returns = [math.log(b / a) for a, b in zip(vals, vals[1:]) if a > 0 and b > 0]
        if len(returns) < 2:
            return 0.0001
        mean = sum(returns) / len(returns)
        return max(math.sqrt(sum((x - mean) ** 2 for x in returns) / (len(returns) - 1)), 1e-6)

    def volatility_per_sqrt_second(self, seconds: float = 60) -> float:
        cutoff = self.values[-1][0]-seconds if self.values else 0
        points = [(t,v) for t,v in self.values if t >= cutoff]
        increments = [(b_t-a_t,math.log(b/a)) for (a_t,a),(b_t,b) in zip(points,points[1:])
                      if b_t > a_t and a > 0 and b > 0]
        elapsed = sum(dt for dt,_ in increments)
        return max(math.sqrt(sum(r*r for _,r in increments)/elapsed),1e-6) if elapsed else .0001


def fair_probability(brti: float, target: float, seconds_left: float, per_second_vol: float) -> float:
    if min(brti, target, seconds_left) <= 0:
        return 0.5
    sigma = max(per_second_vol * math.sqrt(seconds_left), 1e-6)
    z = math.log(brti / target) / sigma
    return min(0.995, max(0.005, 0.5 * (1 + math.erf(z / math.sqrt(2)))))


def taker_fee(price: float, contracts: float, base_rate: float = 0.07,
              series_multiplier: float = 1.0, action: str = "buy",
              balance_precision: float = 0.01) -> float:
    """Kalshi quadratic taker fee including personal-account balance rounding.

    The exchange first ceilings M*0.07*C*P*(1-P) to $0.0001, then floors the
    signed balance change to the user's target precision. Each paper order is
    treated as one accumulated fill; direct members can configure $0.0001.
    """
    if contracts <= 0 or not 0 <= price <= 1:
        return 0.0
    return taker_fee_for_legs([(price,contracts)],base_rate,series_multiplier,
                              action,balance_precision)


def taker_fee_for_legs(legs: list[tuple[float,float]],base_rate: float = .07,
                       series_multiplier: float = 1.0,action: str = "buy",
                       balance_precision: float = .01) -> float:
    valid=[(Decimal(str(p)),Decimal(str(c))) for p,c in legs
           if c>0 and 0<=p<=1]
    if not valid:
        return 0.0
    coefficient=Decimal(str(series_multiplier))*Decimal(str(base_rate))
    raw=sum((coefficient*c*p*(1-p) for p,c in valid),Decimal(0))
    trade_fee = raw.quantize(Decimal(".0001"),rounding=ROUND_CEILING)
    notional=sum((p*c for p,c in valid),Decimal(0))
    precision = max(Decimal(str(balance_precision)),Decimal(".0001"))
    if action == "sell":
        credit = ((notional-trade_fee)/precision).to_integral_value(
            rounding=ROUND_FLOOR)*precision
        return float(max(Decimal(0),notional-credit))
    debit = ((notional+trade_fee)/precision).to_integral_value(
        rounding=ROUND_CEILING)*precision
    return float(max(Decimal(0),debit-notional))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
