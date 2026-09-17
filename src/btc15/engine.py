from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .catalog import (AGENT_KINDS, EDGE_FORMULA, KNOBS, LEGACY_KINDS, NOT_OPPORTUNITIES,
                    OPPORTUNITY_BUCKETS, OPPORTUNITY_DEFINITION, REASON_LABELS,
                    SHARED_TRIGGERS, classify_opportunities, kind_guide, leftover_edge)
from .config import Settings
from .domain import (Market,OrderBook,PriceHistory,f,fair_probability,iso_ts,taker_fee,
                     taker_fee_for_legs)
from .kalshi import ReadOnlyKalshi
from .model import (MicrostructureState,PlattCalibrator,clamp,estimate_probability,
                    logistic,logit)
from .names import display_name
from .policy import (POLICY_FIELDS,bot_templates,defaults as policy_defaults,
                     normalize as normalize_policy,presets as policy_presets)
from .storage import Store


@dataclass
class Experiment:
    name: str
    kind: str
    threshold: float
    cash: float
    bank: float = 0
    realized: float = 0
    current_action: str = "HOLD"
    reason: str = "Waiting for synchronized data"
    last_trade: dict[str, float] = field(default_factory=dict)
    trades_by_market: dict[str, int] = field(default_factory=dict)
    exposure_by_market: dict[str, float] = field(default_factory=dict)
    suggested_size: float = 0
    memory: list[dict[str, Any]] = field(default_factory=list)
    market_realized: dict[str, float] = field(default_factory=dict)
    consecutive_wins: int = 0
    consecutive_losses: int = 0
    last_state_persist: float = 0
    allocated_capital: float = 0
    deployed: bool = False
    policy: dict[str,float] = field(default_factory=dict)
    lifecycle_state: str = "SCANNING"
    waiting_for: str = ""


class Engine:
    def __init__(self, s: Settings, store: Store):
        self.s, self.store = s, store
        self.client = ReadOnlyKalshi(s)
        self.markets: dict[str, Market] = {}
        self.books: dict[str, OrderBook] = {}
        self.current: str | None = None
        self.brti = 0.0
        self.brti_ts = 0.0
        self.brti_received_ts = 0.0
        self.history = PriceHistory()
        self.fast_history = PriceHistory()
        self.flows: dict[str,MicrostructureState] = {}
        self.calibrator = PlattCalibrator(s.model_min_training_markets)
        self.latest_model = None
        self.fee_type = "quadratic"
        self.fees_verified = False
        self.settlement_avg: float | None = None
        self.settlement_count = 0
        self.crossings = 0
        self.last_crossing = 0.0
        self.last_sign: int | None = None
        self.last_eval = 0.0
        self.pending: set[tuple[str, str]] = set()
        self.execution_tasks: set[asyncio.Task] = set()
        self.health = {"kalshi": {"status": "starting", "last_event_ts": 0, "reconnects": 0, "error": ""},
                       "recorder": {"status": "ok", "last_event_ts": time.time(), "reconnects": 0, "error": ""}}
        self.agent_kinds = {
            "fair-value": .030,
            "momentum": .025,
            "late-settlement": .025,
        }
        self.legacy_kinds = LEGACY_KINDS
        self.experiments: dict[str,Experiment] = {}
        self.manual=Experiment("manual-paper","manual",.04,s.bankroll,
                               allocated_capital=s.bankroll,deployed=True,
                               policy=policy_defaults(s))
        self.tasks: list[asyncio.Task] = []
        self.clock = time.time
        self.latency_scale = 1.0
        self.replay_mode = False
        self.record_raw = True
        self.orderbook_sid: int | None = None
        self.orderbook_seq = 0

    async def start(self) -> None:
        try:
            series = await self.client.series()
            self.fee_type = series.get("fee_type", "")
            self.s.fee_multiplier = f(series.get("fee_multiplier"), self.s.fee_multiplier)
            self.fees_verified = True
        except Exception as exc:
            self.health["kalshi"].update(status="error", error=f"Fee discovery: {exc}")
        frozen = {
            "fee_type": self.fee_type, "fee_base_rate": self.s.fee_base_rate,
            "fee_multiplier": self.s.fee_multiplier,
            "balance_precision": self.s.balance_precision,
            "latency_ms": self.s.latency_ms, "bankroll": self.s.bankroll,
            "dollars_per_trade": self.s.dollars_per_trade,
            "min_dollars_per_trade": self.s.min_dollars_per_trade,
            "capital_fraction_per_trade": self.s.capital_fraction_per_trade,
            "profit_bank_rate": self.s.profit_bank_rate,
            "safety_margin": self.s.safety_margin,
            "max_market_exposure":self.s.max_market_exposure,
            "max_total_exposure":self.s.max_total_exposure,
            "max_trades_per_market":self.s.max_trades_per_market,
            "min_liquidity":self.s.min_liquidity,
            "max_spread":self.s.max_spread,
            "stale_seconds":self.s.stale_seconds,
            "min_seconds_remaining":self.s.min_seconds_remaining,
            "cooldown_seconds":self.s.cooldown_seconds,
            "model_min_training_markets":self.s.model_min_training_markets,
            "model_min_confidence":self.s.model_min_confidence,
            "model_uncertainty_penalty":self.s.model_uncertainty_penalty,
            "fractional_kelly":self.s.fractional_kelly,
            "flow_half_life_seconds":self.s.flow_half_life_seconds,
            "min_valid_btc_target":self.s.min_valid_btc_target,
        }
        for key, value in frozen.items():
            await self.store.execute("INSERT OR REPLACE INTO session_config VALUES(?,?)",
                                     (key, json.dumps(value)))
        await self.fit_calibrator()
        await self.restore_agents()
        await self.restore_accounts()
        await self.open_job_windows()
        await self.discover()
        self.tasks = [
            asyncio.create_task(self.client.stream(self.active_tickers, self.on_event, self.on_health)),
            asyncio.create_task(self.rollover_loop()),
        ]

    async def stop(self) -> None:
        for exp in (*self.experiments.values(), self.manual):
            await self.persist_agent_state(exp)
        for task in self.tasks:
            task.cancel()
        for task in self.execution_tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, *self.execution_tasks, return_exceptions=True)

    async def restore_agents(self) -> None:
        rows=await self.store.rows("SELECT * FROM agent_deployments ORDER BY created_ts")
        if not rows:
            legacy=await self.store.rows(
                "SELECT experiment FROM accounts WHERE experiment!='manual-paper'")
            now=self.clock()
            for row in legacy:
                name=row["experiment"]
                kind=self.legacy_kinds.get(name)
                if kind:
                    await self.store.execute("""INSERT OR IGNORE INTO agent_deployments
                        VALUES(?,?,?,?,?,?,?)""",
                        (name,kind,self.agent_kinds[kind],self.s.bankroll,0,now,now))
            rows=await self.store.rows("SELECT * FROM agent_deployments ORDER BY created_ts")
        saved_policies={row["experiment"]:json.loads(row["policy"])
                        for row in await self.store.rows("SELECT * FROM agent_policies")}
        for row in rows:
            kind=self.legacy_kinds.get(row["kind"],row["kind"])
            if row["deployed"] < 0 or kind not in self.agent_kinds:
                continue
            self.experiments[row["experiment"]]=Experiment(
                row["experiment"],kind,row["threshold"],row["allocated_capital"],
                allocated_capital=row["allocated_capital"],deployed=bool(row["deployed"]),
                policy=normalize_policy(saved_policies.get(row["experiment"]),self.s))
        if not self.experiments:
            await self.seed_preset_fleet()

    async def seed_preset_fleet(self) -> None:
        budget = float(self.s.bankroll)
        templates = {item["id"]: item for item in bot_templates(self.s)}
        for name, template_id in (
            ("fair-value", "fair-value"),
            ("momentum", "momentum"),
            ("late-settlement", "late-settlement"),
        ):
            template = templates[template_id]
            await self.create_agent(
                name, template["kind"], budget, template["threshold"], True, template["policy"])

    async def persist_deployment(self,exp: Experiment) -> None:
        now=self.clock()
        await self.store.execute("""INSERT INTO agent_deployments(
            experiment,kind,threshold,allocated_capital,deployed,created_ts,updated_ts)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(experiment) DO UPDATE SET
            kind=excluded.kind,threshold=excluded.threshold,
            allocated_capital=excluded.allocated_capital,deployed=excluded.deployed,
            updated_ts=excluded.updated_ts""",
            (exp.name,exp.kind,exp.threshold,exp.allocated_capital,
             int(exp.deployed),now,now))
        await self.store.execute("INSERT OR REPLACE INTO agent_policies VALUES(?,?,?)",
                                 (exp.name,json.dumps(exp.policy,separators=(",",":")),now))

    async def create_agent(self,name: str,kind: str,budget: float,
                           threshold: float,deployed: bool = True,
                           policy: dict | None = None) -> dict[str,Any]:
        name=name.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,31}",name):
            raise ValueError("Agent name must be 2-32 lowercase letters, numbers, or hyphens")
        kind = self.legacy_kinds.get(kind, kind)
        if kind not in self.agent_kinds:
            raise ValueError(f"Unknown specialist kind: {kind}")
        if not 1<=budget<=10_000:
            raise ValueError("Paper budget must be between $1 and $10,000")
        if not .005<=threshold<=.20:
            raise ValueError("Minimum edge must be between 0.5% and 20%")
        if name in self.experiments or await self.store.one(
                "SELECT experiment FROM agent_deployments WHERE experiment=?",(name,)):
            raise ValueError("Agent name already exists")
        if await self.store.one("SELECT experiment FROM accounts WHERE experiment=?",(name,)):
            raise ValueError("Agent name has historical account data; choose another name")
        preset = policy_presets(self.s).get(kind, {})
        overlay = {key: preset[key] for key in (
            "entry_start_seconds", "entry_stop_seconds", "max_order_dollars",
            "cooldown_seconds", "max_entries_per_market") if key in preset}
        if policy:
            overlay.update(policy)
        exp=Experiment(name,kind,threshold,budget,allocated_capital=budget,deployed=deployed,
                       policy=normalize_policy(overlay or None,self.s))
        self.experiments[name]=exp
        await self.persist_deployment(exp)
        await self.persist_account(exp)
        await self.persist_agent_state(exp)
        return {"ok":True,"name":name,"display_name":display_name(name),
                "kind":kind,"deployed":deployed,"threshold":threshold,
                "allocated_capital":budget,"cash":budget}

    async def control_agent(self,name: str,action: str,
                            budget: float | None = None,
                            threshold: float | None = None,
                            policy: dict | None = None,
                            delta: float | None = None,
                            flatten: bool = False,
                            kind: str | None = None,
                            template_id: str | None = None) -> dict[str,Any]:
        exp=self.experiments.get(name)
        if not exp:
            raise ValueError("Unknown active agent")
        if action=="deploy":
            exp.deployed=True
        elif action=="pause":
            exp.deployed=False
        elif action=="add_cash":
            amount = delta if delta is not None else budget
            if amount is None or not .01<=amount<=10_000:
                raise ValueError("Add between $0.01 and $10,000 paper cash")
            return await self.control_agent(name,"budget",budget=exp.allocated_capital+amount)
        elif action=="budget":
            if budget is None or not 1<=budget<=10_000:
                raise ValueError("Paper budget must be between $1 and $10,000")
            change=budget-exp.allocated_capital
            if change<0 and exp.cash < -change:
                raise ValueError("Cannot withdraw capital currently committed or lost")
            exp.cash += change
            exp.allocated_capital=budget
            await self.persist_account(exp)
        elif action=="threshold":
            if threshold is None or not .005<=threshold<=.20:
                raise ValueError("Minimum edge must be between 0.5% and 20%")
            exp.threshold=threshold
        elif action=="policy":
            exp.policy=normalize_policy(policy,self.s)
        elif action=="job":
            return await self.assign_job(name,kind=kind,template_id=template_id,policy=policy)
        elif action=="retire":
            cashed = await self.flatten_agent(exp) if flatten else 0.0
            open_position=await self.store.one(
                "SELECT 1 found FROM positions WHERE experiment=? AND result IS NULL LIMIT 1",
                (name,))
            if open_position:
                raise ValueError("Still has an open paper position; ask 15 to cash out first")
            exp.deployed=False
            await self.store.execute(
                "UPDATE agent_deployments SET deployed=-1,updated_ts=? WHERE experiment=?",
                (self.clock(),name))
            self.experiments.pop(name,None)
            return {"ok":True,"name":name,"display_name":display_name(name),
                    "status":"retired","cashed_out":cashed}
        else:
            raise ValueError("Action must be deploy, pause, budget, add_cash, threshold, policy, job, or retire")
        await self.persist_deployment(exp)
        return {"ok":True,"name":name,"display_name":display_name(name),
                "status":"deployed" if exp.deployed else "paused",
                "kind":exp.kind,"threshold":exp.threshold,"cash":exp.cash,
                "bank":exp.bank,"allocated_capital":exp.allocated_capital,
                "policy":exp.policy}

    async def assign_job(self, name: str, kind: str | None = None,
                         template_id: str | None = None,
                         policy: dict | None = None) -> dict[str, Any]:
        exp = self.experiments.get(name)
        if not exp:
            raise ValueError("Unknown active agent")
        kind = self.legacy_kinds.get(kind, kind) if kind else None
        template_id = self.legacy_kinds.get(template_id, template_id) if template_id else None
        templates = {item["id"]: item for item in bot_templates(self.s)}
        template = templates.get(template_id) if template_id else None
        if template is None and kind:
            template = next((item for item in templates.values() if item["kind"] == kind), None)
        if template is None and policy is None and kind is None:
            raise ValueError("Choose a template: Fair Value, Momentum, or Late Settlement")
        if template:
            exp.kind = template["kind"]
            exp.threshold = template["threshold"]
            exp.policy = normalize_policy({**template["policy"], **(policy or {})}, self.s)
        elif kind:
            if kind not in self.agent_kinds:
                raise ValueError(f"Unknown job {kind}")
            exp.kind = kind
            if policy:
                exp.policy = normalize_policy(policy, self.s)
        await self.persist_deployment(exp)
        title = kind_guide(exp.kind)["title"]
        return {"ok": True, "name": name, "display_name": display_name(name),
                "kind": exp.kind, "job": title, "threshold": exp.threshold,
                "status": "deployed" if exp.deployed else "paused",
                "cash": exp.cash, "bank": exp.bank,
                "allocated_capital": exp.allocated_capital,
                "policy": exp.policy,
                "reply": f"{display_name(name)} now works the {title} job."}

    def job_window(self, kind: str) -> float:
        return float(AGENT_KINDS.get(kind, {}).get("entry_window_seconds", 840))

    async def open_job_windows(self) -> None:
        for exp in self.experiments.values():
            window = self.job_window(exp.kind)
            if exp.policy.get("entry_start_seconds") != window:
                exp.policy["entry_start_seconds"] = window
                await self.persist_deployment(exp)

    async def flatten_agent(self, exp: Experiment) -> float:
        rows = await self.store.rows(
            "SELECT * FROM positions WHERE experiment=? AND result IS NULL",(exp.name,))
        if not rows:
            return 0.0
        before = exp.cash
        for pos in rows:
            market = self.markets.get(pos["market_ticker"])
            if not market or pos["market_ticker"] not in self.books:
                raise ValueError(f"Cannot cash out {pos['market_ticker']}: no live book")
            self.pending.discard((exp.name, market.ticker))
            await self.execute_after_latency(
                exp, market, "sell", pos["side"], self.clock(), contracts=pos["contracts"])
        leftover = await self.store.one(
            "SELECT 1 found FROM positions WHERE experiment=? AND result IS NULL LIMIT 1",
            (exp.name,))
        if leftover:
            raise ValueError("Cash-out did not flatten the position; wait for a live book")
        return max(0.0, exp.cash - before)

    async def restore_accounts(self) -> None:
        portfolios = {**self.experiments, self.manual.name: self.manual}
        for row in await self.store.rows("SELECT * FROM accounts"):
            if row["experiment"] in portfolios:
                exp = portfolios[row["experiment"]]
                exp.cash, exp.bank, exp.realized = row["cash"], row["bank"], row["realized"]
        for row in await self.store.rows("SELECT * FROM agent_state"):
            if row["experiment"] in portfolios:
                exp = portfolios[row["experiment"]]
                try:
                    state = json.loads(row["state"])
                    exp.memory = state.get("memory", [])[-120:]
                    exp.market_realized = state.get("market_realized", {})
                    exp.consecutive_wins = state.get("consecutive_wins", 0)
                    exp.consecutive_losses = state.get("consecutive_losses", 0)
                except (TypeError, ValueError):
                    pass
        for row in await self.store.rows(
                "SELECT experiment,market_ticker,SUM(cost) exposure FROM positions "
                "WHERE result IS NULL GROUP BY experiment,market_ticker"):
            if row["experiment"] in portfolios:
                portfolios[row["experiment"]].exposure_by_market[row["market_ticker"]] = row["exposure"]
        for row in await self.store.rows(
                "SELECT experiment,market_ticker,COUNT(*) trades,MAX(fill_ts) last_trade "
                "FROM fills WHERE action='buy' GROUP BY experiment,market_ticker"):
            if row["experiment"] in portfolios:
                exp = portfolios[row["experiment"]]
                exp.trades_by_market[row["market_ticker"]] = row["trades"]
                exp.last_trade[row["market_ticker"]] = row["last_trade"]
        for exp in portfolios.values():
            await self.persist_account(exp)

    async def persist_account(self, exp: Experiment) -> None:
        await self.store.execute("INSERT OR REPLACE INTO accounts VALUES(?,?,?,?,?)",
            (exp.name, exp.cash, exp.bank, exp.realized, self.clock()))

    async def persist_agent_state(self, exp: Experiment) -> None:
        state = json.dumps({"memory":exp.memory[-120:],"market_realized":exp.market_realized,
            "consecutive_wins":exp.consecutive_wins,"consecutive_losses":exp.consecutive_losses},
            separators=(",",":"))
        await self.store.execute("INSERT OR REPLACE INTO agent_state VALUES(?,?,?)",
                                 (exp.name,state,self.clock()))
        exp.last_state_persist = self.clock()

    async def fit_calibrator(self) -> None:
        rows = await self.store.rows("""WITH ranked AS (
              SELECT f.p_yes,m.result,m.close_ts,
                ROW_NUMBER() OVER (
                  PARTITION BY f.market_ticker
                  ORDER BY ABS(f.seconds_left-120),f.id) rank
              FROM features f JOIN markets m ON m.ticker=f.market_ticker
              WHERE m.result IN ('yes','no') AND f.data_fresh=1
                AND f.target_valid=1 AND f.yes_bid IS NOT NULL AND f.yes_ask IS NOT NULL)
            SELECT p_yes,result FROM ranked WHERE rank=1 ORDER BY close_ts""")
        self.calibrator.fit([(float(row["p_yes"]),1 if row["result"]=="yes" else 0)
                             for row in rows if row["p_yes"] is not None])

    def apply_realized_pnl(self, exp: Experiment, ticker: str, pnl: float) -> None:
        exp.realized += pnl
        exp.market_realized[ticker] = exp.market_realized.get(ticker, 0)+pnl
        if pnl > 0:
            exp.consecutive_wins += 1
            exp.consecutive_losses = 0
        elif pnl < 0:
            exp.consecutive_losses += 1
            exp.consecutive_wins = 0
        bank_transfer = max(0.0,pnl)*self.s.profit_bank_rate
        exp.cash -= bank_transfer
        exp.bank += bank_transfer

    async def record_closed_trade(self, exp: Experiment, ticker: str, side: str,
                                  contracts: float, entry_notional: float,
                                  exit_notional: float, pnl: float, fees: float,
                                  close_ts: float, close_type: str,
                                  result: str | None = None) -> None:
        source_key = f"{exp.name}:{ticker}:{side}:{close_ts:.6f}:{close_type}"
        await self.store.execute("""INSERT OR IGNORE INTO closed_trades(
            experiment,market_ticker,side,contracts,entry_notional,exit_notional,pnl,
            fees,close_ts,close_type,result,source_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (exp.name,ticker,side,contracts,entry_notional,exit_notional,pnl,fees,
             close_ts,close_type,result,source_key))

    def active_tickers(self) -> list[str]:
        now = self.clock()
        return [m.ticker for m in sorted(self.markets.values(), key=lambda x: x.close_ts)
                if m.close_ts > now and m.target >= self.s.min_valid_btc_target][:2]

    async def discover(self) -> None:
        found: list[dict] = []
        for status in ("open", "unopened"):
            try:
                found += await self.client.markets(status)
            except Exception as exc:
                self.health["kalshi"].update(status="error", error=f"Discovery: {exc}")
        parsed = sorted((Market.from_api(x) for x in found), key=lambda m: m.close_ts)
        now = time.time()
        for market in parsed:
            self.markets[market.ticker] = market
            self.books.setdefault(market.ticker, OrderBook())
            self.flows.setdefault(market.ticker,MicrostructureState(self.s.flow_half_life_seconds))
            await self.store.market(market)
        active = [m for m in parsed if m.close_ts > now and m.open_ts <= now+60
                  and m.target >= self.s.min_valid_btc_target]
        valid_upcoming=[m for m in parsed if m.close_ts>now
                        and m.target>=self.s.min_valid_btc_target]
        next_current=(active[0].ticker if active else
                      (valid_upcoming[0].ticker if valid_upcoming else None))
        if next_current != self.current:
            self.crossings, self.last_crossing, self.last_sign = 0, 0.0, None
            self.settlement_avg,self.settlement_count,self.latest_model=None,0,None
        self.current = next_current

    async def rollover_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            old = set(self.markets)
            old_current = self.current
            await self.discover()
            if set(self.markets) != old or self.current != old_current:
                # Reconnect to guarantee fresh subscriptions and snapshots.
                for task in self.tasks[:1]:
                    task.cancel()
                self.tasks[0] = asyncio.create_task(
                    self.client.stream(self.active_tickers, self.on_event, self.on_health))
            await self.poll_settlements()

    async def poll_settlements(self) -> None:
        for ticker, m in list(self.markets.items()):
            if m.result or time.time() < m.close_ts:
                continue
            try:
                fresh = Market.from_api(await self.client.market(ticker))
                await self.store.market(fresh)
                self.markets[ticker] = fresh
                if fresh.result in ("yes", "no"):
                    await self.settle(ticker, fresh.result)
            except Exception:
                pass

    async def on_health(self, status: str, error: str) -> None:
        h = self.health["kalshi"]
        if status == "reconnecting":
            h["reconnects"] += 1
        elif status == "connected":
            self.orderbook_sid, self.orderbook_seq = None, 0
            for book in self.books.values():
                book.invalidate()
            for flow in self.flows.values():
                flow.reset()
        h.update(status=status, error=error)
        await self.store.execute("INSERT OR REPLACE INTO health VALUES(?,?,?,?,?)",
                                 ("kalshi", status, h["last_event_ts"], h["reconnects"], error))

    async def on_event(self, event: dict, received: float) -> None:
        typ, msg = event.get("type", "unknown"), event.get("msg", {})
        ticker = msg.get("market_ticker")
        source_ts = iso_ts(msg.get("received_at") or msg.get("ts") or msg.get("time"))
        if self.record_raw:
            await self.store.raw("kalshi", typ, ticker, source_ts or None, received, event.get("seq"), event)
        self.health["recorder"].update(status="ok", last_event_ts=received, error="")
        self.health["kalshi"].update(status="connected", last_event_ts=received, error="")
        if typ in ("orderbook_snapshot", "orderbook_delta"):
            self.validate_orderbook_sequence(event)
        if typ == "orderbook_snapshot" and ticker in self.books:
            self.books[ticker].snapshot(msg, event.get("seq", 0))
            self.books[ticker].updated = received
            self.flows.setdefault(ticker,MicrostructureState(self.s.flow_half_life_seconds)).reset(received)
        elif typ == "orderbook_delta" and ticker in self.books:
            self.flows.setdefault(ticker,MicrostructureState(self.s.flow_half_life_seconds)).on_delta(
                msg.get("side",""),f(msg.get("delta_fp",msg.get("delta"))),received)
            if not self.books[ticker].delta(msg, event.get("seq", 0)):
                raise RuntimeError("Order-book sequence gap; forcing fresh snapshot")
            self.books[ticker].updated = received
        elif typ == "trade" and ticker in self.books:
            self.flows.setdefault(ticker,MicrostructureState(self.s.flow_half_life_seconds)).on_trade(
                msg.get("taker_side"),f(msg.get("count_fp",msg.get("count"))),received)
        elif typ in ("cfbenchmarks_value", "cfbenchmarks_value_5hz"):
            await self.on_brti(typ, msg, received)
        elif (typ == "market_lifecycle_v2" and not self.replay_mode
              and msg.get("event_type") in ("determined", "settled")):
            await self.poll_settlements()

    def validate_orderbook_sequence(self, event: dict) -> None:
        """Sequence numbers are global to the order-book subscription SID, not each ticker."""
        sid, seq = event.get("sid"), int(event.get("seq", 0))
        if sid is None or not seq:
            return
        if event.get("type")=="orderbook_snapshot" and seq==1:
            self.orderbook_sid,self.orderbook_seq=sid,seq
            return
        if self.orderbook_sid != sid:
            self.orderbook_sid, self.orderbook_seq = sid, seq
            return
        if self.orderbook_seq and seq != self.orderbook_seq + 1:
            for book in self.books.values():
                book.invalidate()
            raise RuntimeError(
                f"Order-book subscription sequence gap: expected {self.orderbook_seq + 1}, got {seq}")
        self.orderbook_seq = seq

    async def on_brti(self, typ: str, msg: dict, received: float) -> None:
        try:
            raw = json.loads(msg.get("data", "{}")) if isinstance(msg.get("data"), str) else msg.get("data", msg)
            value = f(raw.get("value"))
            source_ts = iso_ts(raw.get("time") or msg.get("received_at")) or received
        except Exception:
            return
        if value <= 0:
            return
        self.fast_history.add(source_ts, value)
        if typ == "cfbenchmarks_value":
            self.brti,self.brti_ts,self.brti_received_ts=value,source_ts,received
            self.history.add(source_ts, value)
            final = msg.get("last_60s_windowed_average_15min")
            self.settlement_avg = f(final.get("value")) if final else None
            self.settlement_count = int(final.get("window_size", 0)) if final else 0
            self.update_crossings(value, source_ts)
            if received - self.last_eval >= 0.9:
                self.last_eval = received
                try:
                    await self.evaluate(received)
                except Exception as exc:
                    self.health["recorder"].update(error=f"evaluate: {exc}")

    def update_crossings(self, value: float, ts: float) -> None:
        m = self.markets.get(self.current or "")
        if not m or not m.target:
            return
        sign = 1 if value >= m.target else -1
        if self.last_sign is not None and sign != self.last_sign:
            self.crossings, self.last_crossing = self.crossings + 1, ts
        self.last_sign = sign

    def features(self, now: float) -> dict[str,Any]:
        m, book = self.markets[self.current], self.books[self.current]
        signal_history = self.fast_history if self.fast_history.values else self.history
        vol = signal_history.volatility_per_sqrt_second()
        left = max(0, m.close_ts - now)
        terminal = fair_probability(self.brti,m.target,left,vol)
        bid, ask = book.yes_bid, book.yes_ask
        # BRTI source time lags the receipt clock. Measure history on the print clock.
        market_clock = signal_history.values[-1][0] if signal_history.values else now
        momentum_5s=signal_history.ret(5,market_clock)
        momentum_30s=signal_history.ret(30,market_clock)
        momentum_60s=signal_history.ret(60,market_clock)
        flow=self.flows.setdefault(self.current,MicrostructureState(self.s.flow_half_life_seconds))
        recent_times=[ts for ts,_ in signal_history.values if ts>=market_clock-900]
        history_seconds=(max(recent_times)-min(recent_times)) if len(recent_times)>1 else 0
        model=estimate_probability(
            spot=self.brti,target=m.target,seconds_left=left,volatility=vol,
            terminal_probability=terminal,settlement_average=self.settlement_avg,
            observation_count=self.settlement_count,momentum_5s=momentum_5s,
            momentum_30s=momentum_30s,momentum_60s=momentum_60s,
            weighted_imbalance=book.weighted_imbalance,
            book_flow=flow.book_flow,trade_flow=flow.trade_flow,crossings=self.crossings,
            market_bid=bid,market_ask=ask,history_seconds=history_seconds,
            calibrator=self.calibrator)
        self.latest_model=model
        return dict(brti=self.brti, target=m.target, seconds_left=left, p_yes=model.p_yes,
                    yes_bid=bid, yes_ask=ask, spread=(ask-bid if ask is not None and bid is not None else None),
                    imbalance=book.imbalance, volatility=vol,
                    momentum_1s=signal_history.ret(1, market_clock), momentum_5s=momentum_5s,
                    momentum_15s=signal_history.ret(15, market_clock), momentum_30s=momentum_30s,
                    momentum_60s=momentum_60s, crossings=self.crossings,
                    since_crossing=(now-self.last_crossing if self.last_crossing else -1),
                    settlement_average=self.settlement_avg, observation_count=self.settlement_count,
                    p_terminal=model.p_terminal,p_settlement=model.p_settlement,
                    p_market=model.p_market,
                    p_trend=model.p_trend,trend_strength=model.trend_strength,
                    horizon_agreement=model.horizon_agreement,
                    uncertainty=model.uncertainty,confidence=model.confidence,
                    disagreement=model.disagreement,regime=model.regime,
                    microprice=book.microprice,weighted_imbalance=book.weighted_imbalance,
                    book_flow=flow.book_flow,trade_flow=flow.trade_flow,
                    expected_settlement=model.expected_settlement,
                    settlement_std=model.settlement_std,model_source=model.source,
                    history_confidence=min(1,history_seconds/60))

    async def evaluate(self, now: float) -> None:
        if not self.current or self.current not in self.books or not self.brti:
            return
        x, book, m = self.features(now), self.books[self.current], self.markets[self.current]
        stale = now-min(self.brti_received_ts,book.updated or 0)>self.s.stale_seconds
        x["target_valid"]=int(m.target>=self.s.min_valid_btc_target)
        x["data_fresh"]=int(not stale and book.valid and x["target_valid"])
        cols = tuple(x[k] for k in ("brti","target","seconds_left","p_yes","yes_bid","yes_ask","spread",
            "imbalance","volatility","momentum_1s","momentum_5s","momentum_15s","momentum_30s",
            "momentum_60s","crossings","since_crossing","settlement_average","observation_count",
            "p_terminal","p_settlement","uncertainty","confidence","regime","microprice",
            "weighted_imbalance","book_flow","trade_flow","expected_settlement","settlement_std",
            "model_source","data_fresh","target_valid","p_trend","trend_strength",
            "horizon_agreement"))
        await self.store.execute("""INSERT INTO features(market_ticker,ts,brti,target,seconds_left,p_yes,
            yes_bid,yes_ask,spread,imbalance,volatility,momentum_1s,momentum_5s,momentum_15s,
            momentum_30s,momentum_60s,crossings,since_crossing,settlement_average,observation_count,
            p_terminal,p_settlement,uncertainty,confidence,regime,microprice,weighted_imbalance,
            book_flow,trade_flow,expected_settlement,settlement_std,model_source,data_fresh,target_valid,
            p_trend,trend_strength,horizon_agreement)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.current, now, *cols))
        for exp in list(self.experiments.values()):
            if not exp.deployed:
                exp.current_action,exp.reason="PAUSED","Not deployed; open positions still settle officially"
                exp.waiting_for="Paused — new buys are off."
                exp.suggested_size=0
                continue
            try:
                await self.decide(exp, m, book, x, now, stale)
            except Exception as exc:
                exp.current_action, exp.reason = "ERROR", str(exc)
        await self.record_equity(now)

    async def record_equity(self, now: float) -> None:
        for exp in list(self.experiments.values()):
            positions = await self.store.rows(
                "SELECT market_ticker,side,contracts FROM positions WHERE experiment=? AND result IS NULL",
                (exp.name,))
            liquidation = 0.0
            for pos in positions:
                book = self.books.get(pos["market_ticker"])
                bid = (book.yes_bid if pos["side"] == "yes" else book.no_bid) if book else None
                if bid is not None:
                    liquidation += pos["contracts"]*bid-self.execution_fee(
                        bid,pos["contracts"],"sell")
            await self.store.execute("INSERT INTO equity(experiment,ts,cash,equity,realized) VALUES(?,?,?,?,?)",
                                     (exp.name, now, exp.cash, exp.cash + liquidation, exp.realized))

    async def decide(self, exp: Experiment, m: Market, book: OrderBook, x: dict, now: float, stale: bool) -> None:
        if await self.maybe_exit(exp, m, book, x, now, stale):
            return
        code, reason = self.wait_reason(exp, m, book, x, stale)
        p_yes = self.agent_probability(exp, x) if not code else None
        side = price = leftover = None
        if p_yes is not None:
            choices = []
            for candidate, probability, quote in (("yes", p_yes, book.yes_ask), ("no", 1 - p_yes, book.no_ask)):
                if quote is None:
                    continue
                fee_pc = self.execution_fee(quote, 1, "buy")
                edge = leftover_edge(probability, quote, fee_pc, self.s.safety_margin, 0)
                if edge is None:
                    continue
                choices.append((edge, candidate, quote, probability))
            if not choices:
                code, reason = "no-quote", REASON_LABELS["no-quote"]
            else:
                leftover, side, price, probability = max(choices)
                if not code:
                    code, reason = self.entry_reason(exp, m, book, x, now, stale, side, leftover, probability, price)
        action = "WAIT" if code and code != "below-edge" else ("HOLD" if code else f"BUY_{side.upper()}")
        if action.startswith("BUY"):
            exp.suggested_size = self.paper_size(exp, leftover or 0)
            if exp.suggested_size <= 0:
                action, code, reason = "HOLD", "cash", REASON_LABELS["cash"]
            else:
                reason = (
                    f"BUY {side.upper()}. Model probability {probability:.0%}. "
                    f"{side.upper()} ask {int(round(price*100))}¢. "
                    f"Estimated edge after fees {leftover:.1%} (need {exp.threshold:.1%})."
                )
                code = "buy"
        exp.lifecycle_state = "ENTERING" if action.startswith("BUY") else (
            exp.lifecycle_state if str(exp.lifecycle_state).startswith("HOLDING") else "SCANNING")
        exp.current_action, exp.reason = action, reason
        exp.waiting_for = "" if action.startswith("BUY") else reason
        exp.memory.append({"ts": now, "market_ticker": m.ticker, "preferred_side": side,
                           "p_yes": p_yes, "net_edge": leftover})
        exp.memory = exp.memory[-120:]
        if now - exp.last_state_persist >= 15:
            await self.persist_agent_state(exp)
        await self.record_decision(exp, m, now, action, side, p_yes, price, leftover, leftover, reason, code)
        if action.startswith("BUY") and (exp.name, m.ticker) not in self.pending:
            self.schedule_execution(exp, m, "buy", side, now, dollars=exp.suggested_size)

    def wait_reason(self, exp: Experiment, m: Market, book: OrderBook, x: dict, stale: bool) -> tuple[str, str]:
        if not exp.deployed:
            return "paused", REASON_LABELS["paused"]
        if stale or not book.valid:
            return "stale", REASON_LABELS["stale"]
        if m.target < self.s.min_valid_btc_target:
            return "invalid-target", REASON_LABELS["invalid-target"]
        if not self.fees_verified:
            return "fees", REASON_LABELS["fees"]
        if book.yes_ask is None and book.no_ask is None:
            return "no-quote", REASON_LABELS["no-quote"]
        return "", ""

    def entry_reason(self, exp: Experiment, m: Market, book: OrderBook, x: dict, now: float,
                     stale: bool, side: str, leftover: float, probability: float,
                     price: float) -> tuple[str, str]:
        window = min(exp.policy.get("entry_start_seconds", self.job_window(exp.kind)),
                     self.job_window(exp.kind))
        if x["seconds_left"] > window:
            title = kind_guide(exp.kind)["title"]
            return "window", (
                f"{title} waits until {window/60:.0f} minutes remain. "
                f"{x['seconds_left']/60:.1f} minutes are left.")
        if x["seconds_left"] < exp.policy["entry_stop_seconds"]:
            return "window", "Too close to settlement for a new buy."
        policy = exp.policy
        if x.get("spread") is None or x["spread"] > policy["max_spread"]:
            return "spread", REASON_LABELS["spread"]
        if book.depth("ask" if side == "yes" else "bid") < policy["min_liquidity"]:
            return "liquidity", REASON_LABELS["liquidity"]
        if now - exp.last_trade.get(m.ticker, 0) < policy["cooldown_seconds"]:
            return "cooldown", REASON_LABELS["cooldown"]
        if exp.trades_by_market.get(m.ticker, 0) >= policy["max_entries_per_market"]:
            return "entries", REASON_LABELS["entries"]
        if exp.exposure_by_market.get(m.ticker, 0) >= policy["max_market_exposure"]:
            return "exposure", REASON_LABELS["exposure"]
        if sum(exp.exposure_by_market.values()) >= policy["max_total_exposure"]:
            return "exposure", REASON_LABELS["exposure"]
        if leftover < exp.threshold:
            return "below-edge", (
                f"Estimated edge {leftover:.1%} is below the required {exp.threshold:.1%}. "
                f"Model probability {probability:.0%}. {side.upper()} ask {int(round(price*100))}¢.")
        return "", ""

    def paper_size(self, exp: Experiment, leftover: float) -> float:
        if leftover <= 0:
            return 0.0
        policy = exp.policy or policy_defaults(self.s)
        market_room = policy["max_market_exposure"] - sum(exp.exposure_by_market.values())
        total_room = policy["max_total_exposure"] - sum(exp.exposure_by_market.values())
        target = min(exp.cash * policy["capital_fraction"], policy["max_order_dollars"],
                     market_room, total_room, exp.cash)
        return round(max(0.0, target), 2)

    async def record_decision(self, exp: Experiment, m: Market, now: float, action: str,
                              side: str | None, p_yes: float | None, price: float | None,
                              raw_edge: float | None, leftover: float | None,
                              reason: str, code: str) -> None:
        await self.store.execute("""INSERT INTO decisions(experiment,market_ticker,ts,action,side,p_yes,
            executable_price,raw_edge,net_edge,reason,reason_code) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (exp.name, m.ticker, now, action, side, p_yes, price, raw_edge, leftover, reason, code))

    def agent_probability(self, exp: Experiment, x: dict) -> float:
        terminal = float(x.get("p_terminal") or x.get("p_yes") or .5)
        settlement = float(x.get("p_settlement") or terminal)
        trend = float(x.get("p_trend") or terminal)
        if exp.kind == "late-settlement":
            return clamp(settlement)
        if exp.kind == "momentum":
            return clamp(.55 * trend + .45 * terminal)
        return clamp(terminal)

    def compound_size(self, working_cash: float) -> float:
        target = working_cash * self.s.capital_fraction_per_trade
        target = max(self.s.min_dollars_per_trade, target)
        return round(max(0.0, min(working_cash, self.s.dollars_per_trade, target)), 2)

    def smart_size(self, working_cash: float, probability: float, price: float,
                   confidence: float, robust_edge: float,
                   policy: dict[str, float] | None = None) -> float:
        exp = Experiment("tmp", "fair-value", .03, working_cash,
                         allocated_capital=working_cash, policy=policy or policy_defaults(self.s))
        return self.paper_size(exp, robust_edge)

    def execution_fee(self, price: float, contracts: float, action: str) -> float:
        return taker_fee(price, contracts, self.s.fee_base_rate, self.s.fee_multiplier,
                         action, self.s.balance_precision)

    def execution_fee_for_legs(self, legs: list[tuple[float, float]], action: str) -> float:
        return taker_fee_for_legs(legs, self.s.fee_base_rate, self.s.fee_multiplier,
                                  action, self.s.balance_precision)

    async def maybe_exit(self, exp: Experiment, m: Market, book: OrderBook, x: dict,
                         now: float, stale: bool) -> bool:
        positions = await self.store.rows(
            "SELECT * FROM positions WHERE experiment=? AND market_ticker=? AND result IS NULL",
            (exp.name, m.ticker))
        if not positions or stale or not book.valid or (exp.name, m.ticker) in self.pending:
            return False
        p_yes = self.agent_probability(exp, x)
        for pos in positions:
            side = pos["side"]
            probability = p_yes if side == "yes" else 1 - p_yes
            bid = book.yes_bid if side == "yes" else book.no_bid
            if bid is None:
                continue
            fee_pc = self.execution_fee(bid, pos["contracts"], "sell") / max(pos["contracts"], 1e-9)
            market_over_fair = bid - fee_pc - probability
            break_even = (pos["cost"] + pos["fees"]) / max(pos["contracts"], 1e-9)
            if market_over_fair >= exp.policy.get("exit_edge", .02) or probability <= .25:
                reason = (
                    f"SELL {side.upper()}. Bid {int(round(bid*100))}¢ versus hold value "
                    f"{int(round(probability*100))}¢."
                    if market_over_fair >= exp.policy.get("exit_edge", .02)
                    else f"SELL {side.upper()}. Model reversed to {probability:.0%}.")
                exp.current_action = f"SELL_{side.upper()}"
                exp.lifecycle_state = "EXITING"
                exp.reason = reason
                await self.record_decision(exp, m, now, exp.current_action, side, p_yes, bid,
                                           market_over_fair, market_over_fair, reason, "sell")
                self.schedule_execution(exp, m, "sell", side, now, contracts=pos["contracts"])
                return True
            exp.lifecycle_state = "HOLDING"
            exp.current_action = "HOLD"
            exp.reason = f"Holding {side.upper()} {pos['contracts']:.2f}. Model {probability:.0%}."
        return False

    def schedule_execution(self, exp: Experiment, m: Market, action: str, side: str,
                           decision_ts: float, dollars: float | None = None,
                           contracts: float | None = None) -> None:
        self.pending.add((exp.name, m.ticker))
        task = asyncio.create_task(
            self.execute_after_latency(exp, m, action, side, decision_ts, dollars, contracts))
        self.execution_tasks.add(task)
        task.add_done_callback(self.execution_tasks.discard)

    def risk_reason(self, exp: Experiment, m: Market, book: OrderBook, x: dict, now: float,
                    stale: bool, side: str, edge: float) -> str:
        code, reason = self.wait_reason(exp, m, book, x, stale)
        if code:
            return reason
        p_yes = self.agent_probability(exp, {"p_terminal": .5, "p_yes": .5,
                                             "p_settlement": .5, "p_trend": .5, **x})
        price = book.yes_ask if side == "yes" else book.no_ask
        leftover = edge
        code, reason = self.entry_reason(exp, m, book, x, now, stale, side, leftover, p_yes, price or 0)
        return reason

    def manual_risk_reason(self,m: Market,book: OrderBook,x: dict,
                           stale: bool,side: str) -> str:
        if stale or not book.valid: return "BLOCKED: executable order book unavailable"
        if m.target < self.s.min_valid_btc_target: return "BLOCKED: invalid BTC target"
        if not self.fees_verified: return "BLOCKED: production fees not verified"
        if self.fee_type not in ("quadratic","quadratic_with_maker_fees",
                                 "quadratic_with_combo_maker_fees"):
            return f"BLOCKED: unsupported fee model {self.fee_type}"
        if x["seconds_left"] < self.s.min_seconds_remaining: return "BLOCKED: too near close"
        if x["spread"] is None or x["spread"] > self.s.max_spread: return "BLOCKED: spread"
        if book.depth("ask" if side=="yes" else "bid") < self.s.min_liquidity:
            return "BLOCKED: insufficient executable liquidity"
        return ""

    async def execute_after_latency(self, exp: Experiment, m: Market, action: str, side: str,
                                    decision_ts: float, dollars: float | None = None,
                                    contracts: float | None = None) -> None:
        try:
            await asyncio.sleep(self.s.latency_ms / 1000 / self.latency_scale)
            book, now = self.books.get(m.ticker), self.clock()
            if not book or not book.valid or now - book.updated > self.s.stale_seconds or now >= m.close_ts:
                exp.reason = "CANCELLED: book stale/closed after latency"
                return
            if action == "buy":
                ask = book.yes_ask if side == "yes" else book.no_ask
                if ask is None: return
                policy=exp.policy or policy_defaults(self.s)
                market_room=policy["max_market_exposure"]-exp.exposure_by_market.get(m.ticker,0)
                total_room=policy["max_total_exposure"]-sum(exp.exposure_by_market.values())
                budget = min(dollars or self.s.min_dollars_per_trade, market_room, total_room, exp.cash)
                desired = budget / max(ask, .01)
                filled,notional,legs=book.walk_buy_details(side,desired)
                if filled <= 0.01:
                    exp.reason = "CANCELLED: insufficient post-latency depth"
                    return
                price = notional / filled
                fee=self.execution_fee_for_legs(legs,"buy")
                if notional + fee > exp.cash: return
                exp.cash -= notional + fee
                exp.trades_by_market[m.ticker] = exp.trades_by_market.get(m.ticker, 0) + 1
                exp.exposure_by_market[m.ticker] = exp.exposure_by_market.get(m.ticker, 0) + notional
                opposite_bid = book.yes_bid if side == "yes" else book.no_bid
                spread_cost = max(0, (price-(opposite_bid or price))*filled)
                await self.store.execute("""INSERT INTO fills(experiment,market_ticker,decision_ts,fill_ts,side,
                    contracts,price,notional,fee,spread_cost,latency_ms,action,execution_legs)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (exp.name,m.ticker,decision_ts,now,side,filled,price,notional,fee,
                     spread_cost,self.s.latency_ms,"buy",json.dumps(legs,separators=(",",":"))))
                await self.store.execute("""INSERT INTO positions VALUES(?,?,?,?,?,?,0,NULL)
                    ON CONFLICT(experiment,market_ticker,side) DO UPDATE SET
                    contracts=CASE WHEN positions.result IS NULL THEN positions.contracts+excluded.contracts
                                   ELSE excluded.contracts END,
                    cost=CASE WHEN positions.result IS NULL THEN positions.cost+excluded.cost
                              ELSE excluded.cost END,
                    fees=CASE WHEN positions.result IS NULL THEN positions.fees+excluded.fees
                              ELSE excluded.fees END,
                    settled_pnl=0,result=NULL""",
                    (exp.name,m.ticker,side,filled,notional,fee))
                exp.lifecycle_state="HOLDING_THESIS"
            else:
                pos = await self.store.one("""SELECT * FROM positions WHERE experiment=?
                    AND market_ticker=? AND side=? AND result IS NULL""", (exp.name,m.ticker,side))
                if not pos: return
                requested = min(contracts or pos["contracts"], pos["contracts"])
                filled,notional,legs=book.walk_sell_details(side,requested)
                if filled <= 0:
                    exp.reason = "CANCELLED: no executable bid after latency"
                    return
                price, fraction = notional/filled, filled/pos["contracts"]
                fee=self.execution_fee_for_legs(legs,"sell")
                allocated_cost, allocated_buy_fees = pos["cost"]*fraction, pos["fees"]*fraction
                pnl = notional-fee-allocated_cost-allocated_buy_fees
                exp.cash += notional-fee
                self.apply_realized_pnl(exp,m.ticker,pnl)
                remaining = pos["contracts"]-filled
                spread_cost = max(0, ((book.yes_ask if side=="yes" else book.no_ask) or price)-price)*filled
                await self.store.execute("""INSERT INTO fills(experiment,market_ticker,decision_ts,fill_ts,side,
                    contracts,price,notional,fee,spread_cost,latency_ms,action,execution_legs)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (exp.name,m.ticker,decision_ts,now,side,filled,price,notional,fee,
                     spread_cost,self.s.latency_ms,"sell",json.dumps(legs,separators=(",",":"))))
                await self.record_closed_trade(exp,m.ticker,side,filled,allocated_cost,
                    notional,pnl,allocated_buy_fees+fee,now,"exit")
                if remaining <= 1e-9:
                    await self.store.execute("""DELETE FROM positions WHERE experiment=?
                        AND market_ticker=? AND side=?""", (exp.name,m.ticker,side))
                    exp.lifecycle_state="SCANNING"
                else:
                    await self.store.execute("""UPDATE positions SET contracts=?,cost=?,fees=?
                        WHERE experiment=? AND market_ticker=? AND side=?""",
                        (remaining,pos["cost"]-allocated_cost,pos["fees"]-allocated_buy_fees,
                         exp.name,m.ticker,side))
                    exp.lifecycle_state="HOLDING_THESIS"
                exp.exposure_by_market[m.ticker] = max(
                    0, exp.exposure_by_market.get(m.ticker,0)-allocated_cost)
                if exp.exposure_by_market[m.ticker] <= 1e-9:
                    exp.exposure_by_market.pop(m.ticker,None)
            exp.last_trade[m.ticker] = now
            await self.persist_account(exp)
            await self.persist_agent_state(exp)
        finally:
            self.pending.discard((exp.name, m.ticker))

    async def settle(self, ticker: str, result: str) -> None:
        rows = await self.store.rows("SELECT * FROM positions WHERE market_ticker=? AND result IS NULL", (ticker,))
        portfolios = {**self.experiments, self.manual.name: self.manual}
        for pos in rows:
            payout = pos["contracts"] if pos["side"] == result else 0
            pnl = payout - pos["cost"] - pos["fees"]
            total_pnl = pos["settled_pnl"] + pnl
            exp = portfolios.get(pos["experiment"])
            if exp:
                exp.cash += payout
                self.apply_realized_pnl(exp,ticker,pnl)
                exp.exposure_by_market.pop(ticker, None)
                exp.lifecycle_state="SETTLED"
                await self.record_closed_trade(exp,ticker,pos["side"],pos["contracts"],
                    pos["cost"],payout,pnl,pos["fees"],self.clock(),"settlement",result)
                await self.persist_account(exp)
                await self.persist_agent_state(exp)
            await self.store.execute("""UPDATE positions SET settled_pnl=?,result=?
                WHERE experiment=? AND market_ticker=? AND side=?""",
                (total_pnl,result,pos["experiment"],ticker,pos["side"]))
        await self.fit_calibrator()

    def research_suggestion(self, x: dict, book: OrderBook) -> dict[str, Any]:
        if not self.brti or not book.valid:
            return {"action":"WAIT","side":None,"edge":None,"price":None,
                    "max_price":None,"size":0,"reason":"Waiting for synchronized BRTI and order book"}
        options = []
        for side, probability, ask in (
                ("yes",x["p_yes"],book.yes_ask),("no",1-x["p_yes"],book.no_ask)):
            if ask is not None:
                fee = self.execution_fee(ask,1,"buy")
                edge = probability-ask-fee-self.s.safety_margin-x.get("uncertainty",0)
                options.append((edge,side,ask,probability,fee))
        if not options:
            return {"action":"WAIT","side":None,"edge":None,"price":None,
                    "max_price":None,"size":0,"reason":"No executable two-sided quote"}
        edge,side,ask,probability,fee = max(options)
        threshold = self.manual.threshold
        size = self.smart_size(self.manual.cash,probability,ask,x.get("confidence",1),edge)
        actionable = edge >= threshold
        return {"action":f"PAPER BUY {side.upper()}" if actionable else "HOLD",
                "side":side,"edge":edge,"price":ask,
                "max_price":max(0.01,probability-fee-self.s.safety_margin
                                -x.get("uncertainty",0)-threshold),
                "size":size if actionable else 0,
                "reason":f"Fair {probability:.1%} vs executable {ask:.1%}; "
                         f"net edge {edge:.1%}, required {threshold:.1%}"}

    async def manual_order(self, action: str, side: str, amount: float) -> dict[str, Any]:
        if action not in ("buy","sell") or side not in ("yes","no"):
            raise ValueError("action must be buy/sell and side must be yes/no")
        if not self.current:
            raise ValueError("No active market")
        m, book, now = self.markets[self.current], self.books[self.current], self.clock()
        x = self.features(now)
        stale = now-min(self.brti_ts,book.updated or 0)>self.s.stale_seconds
        if stale or not book.valid:
            raise ValueError("Paper order blocked: stale or unsynchronized market data")
        if action == "buy":
            if amount < self.s.min_dollars_per_trade:
                raise ValueError(f"Minimum paper buy is ${self.s.min_dollars_per_trade:.2f}")
            if amount > self.manual.cash:
                raise ValueError(f"Only ${self.manual.cash:.2f} manual paper cash is available")
            blocked=self.manual_risk_reason(m,book,x,stale,side)
            if blocked:
                raise ValueError(blocked)
            self.manual.current_action=f"MANUAL_BUY_{side.upper()}"
            self.manual.reason=f"Manual paper order ${amount:.2f}; real execution disabled"
            self.schedule_execution(self.manual,m,"buy",side,now,dollars=amount)
        else:
            pos = await self.store.one("""SELECT * FROM positions WHERE experiment=? AND
                market_ticker=? AND side=? AND result IS NULL""",
                (self.manual.name,m.ticker,side))
            if not pos:
                raise ValueError(f"No open manual {side.upper()} paper position")
            bid = book.yes_bid if side=="yes" else book.no_bid
            requested = pos["contracts"] if amount <= 0 else min(pos["contracts"],amount/max(bid or .01,.01))
            self.manual.current_action=f"MANUAL_SELL_{side.upper()}"
            self.manual.reason="Manual paper exit; real execution disabled"
            self.schedule_execution(self.manual,m,"sell",side,now,contracts=requested)
        return {"accepted":True,"paper_only":True,"action":action,"side":side,
                "message":"Queued against the post-latency observable book"}

    def catalog(self) -> dict[str, Any]:
        beginner = [f for f in POLICY_FIELDS if f.get("beginner")]
        advanced = [f for f in POLICY_FIELDS if not f.get("beginner")]
        return {"kinds": [kind_guide(kind) for kind in AGENT_KINDS],
                "knobs": KNOBS, "policy_fields": POLICY_FIELDS,
                "beginner_fields": beginner, "advanced_fields": advanced,
                "presets": policy_presets(self.s), "templates": bot_templates(self.s),
                "edge_formula": EDGE_FORMULA,
                "opportunity_definition": OPPORTUNITY_DEFINITION,
                "opportunity_formula": EDGE_FORMULA,
                "not_opportunities": NOT_OPPORTUNITIES,
                "opportunity_buckets": OPPORTUNITY_BUCKETS,
                "triggers": SHARED_TRIGGERS,
                "compiler_contract": {
                    "version": 3,
                    "controller": "FYFTEN",
                    "purpose": "FYFTEN is a text chatbot that maps chat onto validated bot tools. Bots stay deterministic.",
                    "required": ["template_id", "name", "budget"],
                    "optional": ["threshold", "policy", "deployed"],
                    "allowed_kinds": list(self.agent_kinds),
                    "rule": "Unknown fields are rejected or ignored by policy normalization; no executable code.",
                }}

    async def agent_dossier(self, name: str) -> dict[str, Any]:
        exp = self.experiments.get(name)
        if not exp:
            raise ValueError("Unknown active agent")
        now = self.clock()
        fills = await self.store.rows(
            "SELECT * FROM fills WHERE experiment=? ORDER BY fill_ts DESC LIMIT 80",(name,))
        closed = await self.store.rows(
            "SELECT * FROM closed_trades WHERE experiment=? ORDER BY close_ts DESC LIMIT 80",(name,))
        decisions = await self.store.rows(
            "SELECT ts,action,side,p_yes,executable_price,raw_edge,net_edge,reason "
            "FROM decisions WHERE experiment=? ORDER BY id DESC LIMIT 80",(name,))
        curve = await self.store.rows(
            "SELECT ts,cash,equity,realized FROM equity WHERE experiment=? ORDER BY ts DESC LIMIT 480",(name,))
        stats = await self.store.one("""SELECT COUNT(*) trades,COALESCE(SUM(fee),0) fees,
            COALESCE(SUM(spread_cost),0) spread_cost FROM fills WHERE experiment=?""",(name,))
        settled = await self.store.one("""SELECT COUNT(*) settled,
            SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins,
            SUM(CASE WHEN pnl<=0 THEN 1 ELSE 0 END) losses,
            COALESCE(SUM(pnl),0) net_pnl FROM closed_trades WHERE experiment=?""",(name,))
        breakdown_rows = await self.store.rows(
            "SELECT side,pnl,close_type FROM closed_trades WHERE experiment=?",(name,))
        def tally(key, value_fn):
            bins: dict[str, list] = {}
            for row in breakdown_rows:
                label = value_fn(row)
                if label is None:
                    continue
                bins.setdefault(label,[0,0.0]); bins[label][0]+=1; bins[label][1]+=row["pnl"] or 0
            return [{"bucket":k,"n":v[0],"net_pnl":v[1]} for k,v in bins.items()]
        breakdowns = {
            "side": tally("side", lambda r: (r["side"] or "").upper() or None),
            "close_type": tally("close_type", lambda r: r["close_type"]),
            "regime": tally("regime", lambda r: None),
            "time": tally("time", lambda r: None),
        }
        curve = list(reversed(curve))
        step = max(1, len(curve)//240)
        sampled = curve[::step]
        open_rows = await self.store.rows(
            "SELECT * FROM positions WHERE experiment=? AND result IS NULL",(name,))
        holdings, liquidation, cost, position = self.mark_holdings(open_rows)
        return {"name":name,"kind":exp.kind,"guide":kind_guide(exp.kind),"knobs":KNOBS,
                "policy_fields":POLICY_FIELDS,"policy":exp.policy,
                "triggers":SHARED_TRIGGERS,"cash":exp.cash,"bank":exp.bank,
                "allocated_capital":exp.allocated_capital,"deployed":exp.deployed,
                "threshold":exp.threshold,"realized":exp.realized,"action":exp.current_action,
                "state":exp.lifecycle_state,"reason":exp.reason,
                "suggested_size":exp.suggested_size,
                "position":position,"holdings":holdings,
                "contracts":sum(row["contracts"] or 0 for row in open_rows),
                "cost":cost,"unrealized":liquidation-cost,
                "streak":(f"W{exp.consecutive_wins}" if exp.consecutive_wins else
                          f"L{exp.consecutive_losses}" if exp.consecutive_losses else "—"),
                "fills":fills[::-1],"closed_trades":closed[::-1],
                "decisions":decisions[::-1],"equity_curve":sampled,
                "stats":{**(stats or {}),**(settled or {})},"breakdowns":breakdowns,
                "why_not": await self.why_not_traded(name),
                "as_of":now}

    async def why_not_traded(self, name: str, seconds: float = 900) -> dict[str, Any]:
        since = self.clock() - seconds
        rows = await self.store.rows(
            """SELECT COALESCE(reason_code,'') code, reason, COUNT(*) n
               FROM decisions WHERE experiment=? AND ts>=? AND action IN ('HOLD','WAIT')
               GROUP BY code, reason ORDER BY n DESC LIMIT 12""", (name, since))
        counts: dict[str, int] = {}
        for row in rows:
            code = row["code"] or "other"
            counts[code] = counts.get(code, 0) + row["n"]
        labels = [{"code": code, "label": REASON_LABELS.get(code, reason), "n": n}
                  for code, n, reason in
                  ((row["code"] or "other", row["n"], row["reason"]) for row in rows)]
        collapsed: dict[str, dict] = {}
        for item in labels:
            bucket = collapsed.setdefault(item["code"], {"code": item["code"],
                                                         "label": item["label"], "n": 0})
            bucket["n"] += item["n"]
        ordered = sorted(collapsed.values(), key=lambda row: -row["n"])
        return {"window_seconds": seconds, "counts": ordered}

    def mark_holdings(self, rows: list[dict]) -> tuple[list[dict], float, float, str]:
        holdings: list[dict] = []
        liquidation = 0.0
        cost_sum = 0.0
        parts: list[str] = []
        for pos in rows:
            book = self.books.get(pos["market_ticker"])
            bid = (book.yes_bid if pos["side"] == "yes" else book.no_bid) if book else None
            contracts = pos["contracts"] or 0
            cost = (pos["cost"] or 0) + (pos["fees"] or 0)
            mark = None
            if bid is not None and contracts:
                mark = contracts*bid - self.execution_fee(bid, contracts, "sell")
                liquidation += mark
            cost_sum += cost
            parts.append(f'{pos["side"].upper()} {contracts:.2f}')
            holdings.append({
                "market_ticker": pos["market_ticker"], "side": pos["side"],
                "contracts": contracts, "cost": cost,
                "avg_price": cost/contracts if contracts else None,
                "mark": mark, "unrealized": (mark-cost) if mark is not None else None,
            })
        return holdings, liquidation, cost_sum, " + ".join(parts) or "FLAT"

    async def snapshot(self) -> dict[str, Any]:
        if not self.current or self.current not in self.markets:
            return {"banner":"PAPER TRADING — REAL EXECUTION DISABLED","ready":False,
                    "health":self.health, "catalog": self.catalog(),
                    "agent_kinds":[{"kind":kind,"default_threshold":threshold,
                                    "title":kind_guide(kind)["title"],
                                    "summary":kind_guide(kind)["summary"]}
                                   for kind,threshold in self.agent_kinds.items()],
                    "strategies":[], "manual":{"cash":self.manual.cash,"position":"FLAT"}}
        m,book,now=self.markets[self.current],self.books[self.current],self.clock()
        x = self.features(now) if self.brti else {
            "brti": None, "target": m.target, "seconds_left": max(0, m.close_ts-now),
            "p_yes": None, "yes_bid": book.yes_bid, "yes_ask": book.yes_ask,
            "spread": (book.yes_ask-book.yes_bid
                       if book.yes_ask is not None and book.yes_bid is not None else None),
            "imbalance": book.imbalance, "volatility": None, "momentum_1s": None,
            "momentum_5s": None, "momentum_15s": None, "momentum_30s": None,
            "momentum_60s": None, "crossings": self.crossings, "since_crossing": -1,
            "settlement_average": self.settlement_avg, "observation_count": self.settlement_count,
            "p_terminal":None,"p_settlement":None,"p_trend":None,"p_market":None,
            "trend_strength":0,"horizon_agreement":0,"uncertainty":None,"confidence":0,
            "history_confidence":0,"disagreement":0,
            "regime":"warming-up","microprice":book.microprice,
            "weighted_imbalance":book.weighted_imbalance,"book_flow":0,"trade_flow":0,
            "expected_settlement":None,"settlement_std":None,"model_source":"warming-up",
        }
        fills = await self.store.rows("SELECT * FROM fills WHERE market_ticker=? ORDER BY fill_ts", (m.ticker,))
        position_rows = await self.store.rows(
            "SELECT * FROM positions WHERE result IS NULL")
        positions: dict[str, list[dict]] = {}
        for row in position_rows:
            positions.setdefault(row["experiment"], []).append(row)
        latest_rows = await self.store.rows("""SELECT d.* FROM decisions d JOIN
            (SELECT experiment,MAX(id) id FROM decisions WHERE market_ticker=? GROUP BY experiment) x
            ON d.id=x.id""", (m.ticker,))
        latest = {row["experiment"]: row for row in latest_rows}
        strategies = []
        aggregate_equity = 0.0
        for exp in self.experiments.values():
            holdings, liquidation, cost, position = self.mark_holdings(positions.get(exp.name, []))
            contracts = sum(row["contracts"] or 0 for row in holdings)
            equity, unrealized = exp.cash + liquidation, liquidation - cost
            aggregate_equity += equity+exp.bank
            decision = latest.get(exp.name, {})
            recent_memory=[row for row in exp.memory
                           if row.get("market_ticker")==m.ticker][-10:]
            cached_side = decision.get("side")
            cached_persistence = (sum(row.get("preferred_side")==cached_side for row in recent_memory)/
                                  len(recent_memory)) if recent_memory and cached_side else 0
            strategies.append({"name":exp.name,"display_name":display_name(exp.name),
                "kind":exp.kind,"threshold":exp.threshold,"cash":exp.cash,
                "bank":exp.bank,"total":equity+exp.bank,
                "allocated_capital":exp.allocated_capital,"deployed":exp.deployed,
                "policy":exp.policy,
                "return_on_allocation":((equity+exp.bank-exp.allocated_capital)/exp.allocated_capital
                                        if exp.allocated_capital else None),
                "equity":equity,"unrealized":unrealized,"realized":exp.realized,
                "action":exp.current_action,"state":exp.lifecycle_state,
                "reason":exp.reason,"contracts":contracts,"cost":cost,
                "position":position,"holdings":holdings,
                "entry_price":cost/contracts if contracts else None,
                "p_yes":decision.get("p_yes"),"net_edge":decision.get("net_edge"),
                "waiting_for":exp.waiting_for,
                "trades":sum(exp.trades_by_market.values()),
                "pnl":exp.realized,
                "suggested_size":exp.suggested_size,"cache_samples":len(exp.memory),
                "signal_persistence":cached_persistence,
                "market_realized":exp.market_realized.get(m.ticker,0),
                "streak":(f"W{exp.consecutive_wins}" if exp.consecutive_wins else
                          f"L{exp.consecutive_losses}" if exp.consecutive_losses else "—")})
        manual_holdings, manual_liquidation, manual_cost, manual_positions = self.mark_holdings(
            positions.get(self.manual.name, []))
        manual_equity = self.manual.cash+manual_liquidation
        suggestion = self.research_suggestion(x,book)
        opportunities = classify_opportunities(
            seconds_left=x.get("seconds_left") or 0,
            fresh=bool(book.valid and self.brti and x.get("data_fresh",1)),
            yes_ask=book.yes_ask,no_ask=book.no_ask,
            p_settlement=x.get("p_settlement"),p_trend=x.get("p_trend"),
            p_terminal=x.get("p_terminal"),
            fee_yes=self.execution_fee(book.yes_ask,1,"buy") if book.yes_ask is not None else 0,
            fee_no=self.execution_fee(book.no_ask,1,"buy") if book.no_ask is not None else 0,
            safety=self.s.safety_margin,uncertainty=x.get("uncertainty") or 0)
        stats = await self.store.one("""SELECT
            (SELECT COUNT(*) FROM raw_events) raw_events,
            (SELECT COUNT(*) FROM decisions) decisions,
            (SELECT COUNT(*) FROM fills) fills,
            (SELECT COUNT(*) FROM markets) markets""")
        recent_decisions = await self.store.rows("""SELECT experiment,ts,action,side,p_yes,
            executable_price,net_edge,reason FROM decisions WHERE market_ticker=?
            ORDER BY id DESC LIMIT 12""", (m.ticker,))
        yes_bids = [{"price":p,"size":q} for p,q in sorted(book.yes.items(), reverse=True)[:8]]
        yes_asks = [{"price":p,"size":q} for p,q in sorted(book.no.items())[:8]]
        return {"banner":"PAPER TRADING — REAL EXECUTION DISABLED","ready":True,
                "market":{**m.__dict__,"raw":None},"features":x,"book":{"yes_bid":book.yes_bid,
                "yes_ask":book.yes_ask,"no_bid":book.no_bid,"no_ask":book.no_ask,
                "bid_depth":book.depth("bid"),"ask_depth":book.depth("ask"),"valid":book.valid,
                "microprice":book.microprice,"weighted_imbalance":book.weighted_imbalance,
                "yes_bids":yes_bids,"yes_asks":yes_asks},
                "session":{"aggregate_equity":aggregate_equity,
                    "starting_capital":sum(exp.allocated_capital for exp in self.experiments.values()),
                    "agent_count":len(self.experiments),
                    "deployed_agents":sum(exp.deployed for exp in self.experiments.values()),
                    "capital_fraction_per_trade":self.s.capital_fraction_per_trade,
                    "hard_trade_cap":self.s.dollars_per_trade,**(stats or {})},
                "strategies":strategies,"fills":fills[-20:],
                "catalog":self.catalog(),
                "agent_kinds":[{"kind":kind,"default_threshold":threshold,
                                "title":kind_guide(kind)["title"],
                                "summary":kind_guide(kind)["summary"]}
                               for kind,threshold in self.agent_kinds.items()],
                "manual":{"cash":self.manual.cash,"bank":self.manual.bank,
                    "equity":manual_equity,"total":manual_equity+self.manual.bank,
                    "unrealized":manual_liquidation-manual_cost,"position":manual_positions,
                    "holdings":manual_holdings,
                    "action":self.manual.current_action,"reason":self.manual.reason},
                "suggestion":suggestion,
                "opportunities":opportunities,
                "opportunity_definition":OPPORTUNITY_DEFINITION,
                "opportunity_formula":EDGE_FORMULA,
                "not_opportunities":NOT_OPPORTUNITIES,
                "model":{"calibration":self.calibrator.status()},
                "recent_decisions":recent_decisions,"health":self.health}
