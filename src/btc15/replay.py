from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import aiosqlite

from .config import Settings
from .domain import Market
from .engine import Engine
from .storage import Store


async def run(input_path: Path, output_path: Path, speed: float,
              market_limit: int = 0,copy_raw: bool = True) -> None:
    if input_path.resolve() == output_path.resolve():
        raise ValueError("Replay output must differ from the immutable input recording")
    output_path.unlink(missing_ok=True)
    out = Store(output_path,autocommit=False)
    await out.open()
    async with aiosqlite.connect(input_path) as source:
        source.row_factory = aiosqlite.Row
        config = {}
        try:
            async with source.execute("SELECT key,value FROM session_config") as cur:
                config = {r["key"]: json.loads(r["value"]) for r in await cur.fetchall()}
        except aiosqlite.OperationalError:
            pass
        allowed = {k:config[k] for k in ("fee_base_rate","fee_multiplier","balance_precision",
                   "latency_ms","bankroll",
                   "dollars_per_trade","min_dollars_per_trade","profit_bank_rate",
                   "capital_fraction_per_trade","safety_margin",
                   "max_market_exposure","max_total_exposure","max_trades_per_market",
                   "min_liquidity","max_spread","stale_seconds",
                   "min_seconds_remaining","cooldown_seconds",
                   "model_min_training_markets","model_min_confidence",
                   "model_uncertainty_penalty","fractional_kelly",
                   "flow_half_life_seconds","min_valid_btc_target") if k in config}
        engine = Engine(Settings(db_path=output_path, **allowed), out)
        engine.fee_type = config.get("fee_type", "quadratic")
        engine.fees_verified = "fee_multiplier" in config
        clock = [0.0]
        engine.clock = lambda: clock[0]
        engine.latency_scale = max(speed, 0.01)
        engine.replay_mode = True
        engine.record_raw = copy_raw
        policies = {}
        try:
            async with source.execute("SELECT experiment,policy FROM agent_policies") as cur:
                policies = {row["experiment"]:json.loads(row["policy"])
                            for row in await cur.fetchall()}
            async with source.execute("""SELECT experiment,kind,threshold,allocated_capital,deployed
                FROM agent_deployments WHERE deployed>=0 ORDER BY created_ts""") as cur:
                for row in await cur.fetchall():
                    kind=engine.legacy_kinds.get(row["kind"],row["kind"])
                    if kind in engine.agent_kinds:
                        await engine.create_agent(
                            row["experiment"],kind,row["allocated_capital"],row["threshold"],
                            bool(row["deployed"]),policies.get(row["experiment"]))
        except aiosqlite.OperationalError:
            pass
        market_sql=("SELECT metadata,result,expiration_value FROM markets ORDER BY close_ts"
                    if market_limit<=0 else
                    """SELECT metadata,result,expiration_value FROM markets
                       WHERE result IN ('yes','no') ORDER BY close_ts DESC LIMIT ?""")
        args=() if market_limit<=0 else (market_limit,)
        async with source.execute(market_sql,args) as cur:
            market_rows=await cur.fetchall()
        replay_markets=[]
        for row in market_rows:
            market=Market.from_api(json.loads(row["metadata"]))
            market.result=row["result"] or market.result
            market.expiration_value=row["expiration_value"] or market.expiration_value
            replay_markets.append(market)
        replay_markets.sort(key=lambda market:market.close_ts)
        from .domain import OrderBook
        for market in replay_markets:
            engine.markets[market.ticker] = market
            engine.books[market.ticker] = OrderBook()
            await out.market(market)
        replay_start=(replay_markets[0].open_ts-1200 if market_limit>0 and replay_markets else None)
        replay_end=(replay_markets[-1].close_ts+60 if market_limit>0 and replay_markets else None)
        previous = None
        settled: set[str] = set()
        event_sql="""SELECT payload,received_ts FROM raw_events
            WHERE source='kalshi'"""
        event_args=()
        if replay_start is not None and replay_end is not None:
            event_sql+=" AND received_ts BETWEEN ? AND ?"
            event_args=(replay_start,replay_end)
        event_sql+=" ORDER BY received_ts,id"
        async with source.execute(event_sql,event_args) as cur:
            async for row in cur:
                ts = row["received_ts"]
                if previous is not None:
                    delay=min(1,max(0,ts-previous)/max(speed,.01))
                    # Sub-millisecond sleeps are expensive timer yields on Windows and
                    # can turn a high-speed replay into minutes of idle scheduler time.
                    if delay>=.001:
                        await asyncio.sleep(delay)
                clock[0] = ts
                event = json.loads(row["payload"])
                ticker = event.get("msg", {}).get("market_ticker")
                candidates = [m for m in engine.markets.values() if m.open_ts <= ts < m.close_ts]
                if candidates:
                    engine.current = min(candidates, key=lambda m: m.close_ts).ticker
                try:
                    await engine.on_event(event, ts)
                except RuntimeError:
                    # Recorded stream may contain a reconnect gap; next snapshot restores validity.
                    pass
                for market in engine.markets.values():
                    if market.ticker not in settled and market.close_ts <= ts and market.result in ("yes","no"):
                        await engine.settle(market.ticker, market.result)
                        settled.add(market.ticker)
                previous = ts
    await asyncio.sleep(engine.s.latency_ms / 1000 / max(speed, .01) + .1)
    for market in engine.markets.values():
        if market.result in ("yes", "no"):
            await engine.settle(market.ticker, market.result)
    await out.close()
    print(f"Replay complete: {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay immutable BTC15 raw events through the V2 pipeline")
    parser.add_argument("input", type=Path, help="Overnight paper.db recording")
    parser.add_argument("--output", type=Path, default=Path("data/replay.db"))
    parser.add_argument("--speed", type=float, default=100.0)
    parser.add_argument("--markets", type=int, default=0,
                        help="Replay only the N most recent contiguous markets (0 means all)")
    parser.add_argument("--copy-raw",action="store_true",
                        help="Duplicate source events into output (slower; source remains immutable)")
    args = parser.parse_args()
    asyncio.run(run(args.input,args.output,args.speed,args.markets,args.copy_raw))


if __name__ == "__main__":
    main()
