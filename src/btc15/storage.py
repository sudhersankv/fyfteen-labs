from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import aiosqlite


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS raw_events (
 id INTEGER PRIMARY KEY, source TEXT NOT NULL, type TEXT NOT NULL, market_ticker TEXT,
 source_ts REAL, received_ts REAL NOT NULL, seq INTEGER, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_raw_time ON raw_events(received_ts,id);
CREATE TABLE IF NOT EXISTS markets (
 ticker TEXT PRIMARY KEY, event_ticker TEXT, target REAL, open_ts REAL, close_ts REAL,
 condition TEXT, status TEXT, result TEXT, expiration_value REAL, metadata TEXT);
CREATE TABLE IF NOT EXISTS features (
 id INTEGER PRIMARY KEY, market_ticker TEXT, ts REAL, brti REAL, target REAL, seconds_left REAL,
 p_yes REAL, yes_bid REAL, yes_ask REAL, spread REAL, imbalance REAL, volatility REAL,
 momentum_1s REAL, momentum_5s REAL, momentum_15s REAL, momentum_30s REAL, momentum_60s REAL,
 crossings INTEGER, since_crossing REAL, settlement_average REAL, observation_count INTEGER,
 p_terminal REAL, p_settlement REAL, uncertainty REAL, confidence REAL, regime TEXT,
 microprice REAL, weighted_imbalance REAL, book_flow REAL, trade_flow REAL,
 expected_settlement REAL, settlement_std REAL, model_source TEXT,
 data_fresh INTEGER, target_valid INTEGER, p_trend REAL, trend_strength REAL,
 horizon_agreement REAL);
CREATE INDEX IF NOT EXISTS ix_features_market_horizon
 ON features(market_ticker,seconds_left,id);
CREATE TABLE IF NOT EXISTS decisions (
 id INTEGER PRIMARY KEY, experiment TEXT, market_ticker TEXT, ts REAL, action TEXT, side TEXT,
 p_yes REAL, executable_price REAL, raw_edge REAL, net_edge REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS fills (
 id INTEGER PRIMARY KEY, experiment TEXT, market_ticker TEXT, decision_ts REAL, fill_ts REAL,
 side TEXT, contracts REAL, price REAL, notional REAL, fee REAL, spread_cost REAL, latency_ms INTEGER,
 action TEXT NOT NULL DEFAULT 'buy', fee_original REAL, execution_legs TEXT);
CREATE TABLE IF NOT EXISTS positions (
 experiment TEXT, market_ticker TEXT, side TEXT, contracts REAL, cost REAL, fees REAL,
 settled_pnl REAL DEFAULT 0, result TEXT, PRIMARY KEY(experiment,market_ticker,side));
CREATE TABLE IF NOT EXISTS equity (
 id INTEGER PRIMARY KEY, experiment TEXT, ts REAL, cash REAL, equity REAL, realized REAL);
CREATE TABLE IF NOT EXISTS health (
 component TEXT PRIMARY KEY, status TEXT, last_event_ts REAL, reconnects INTEGER DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS session_config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
 experiment TEXT PRIMARY KEY, cash REAL NOT NULL, bank REAL NOT NULL, realized REAL NOT NULL,
 updated_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS closed_trades (
 id INTEGER PRIMARY KEY, experiment TEXT NOT NULL, market_ticker TEXT NOT NULL, side TEXT NOT NULL,
 contracts REAL NOT NULL, entry_notional REAL NOT NULL, exit_notional REAL NOT NULL,
 pnl REAL NOT NULL, fees REAL NOT NULL, close_ts REAL NOT NULL, close_type TEXT NOT NULL,
 result TEXT, source_key TEXT UNIQUE);
CREATE INDEX IF NOT EXISTS ix_closed_agent_time ON closed_trades(experiment,close_ts);
CREATE TABLE IF NOT EXISTS agent_state (
 experiment TEXT PRIMARY KEY, state TEXT NOT NULL, updated_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS agent_deployments (
 experiment TEXT PRIMARY KEY, kind TEXT NOT NULL, threshold REAL NOT NULL,
 allocated_capital REAL NOT NULL, deployed INTEGER NOT NULL DEFAULT 0,
 created_ts REAL NOT NULL, updated_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS agent_policies (
 experiment TEXT PRIMARY KEY, policy TEXT NOT NULL, updated_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS repair_audit (
 id INTEGER PRIMARY KEY, repaired_ts REAL NOT NULL, description TEXT NOT NULL, details TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: Path, autocommit: bool = True):
        self.path = path
        self.autocommit = autocommit
        self.db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        await self.db.executescript(SCHEMA)
        columns = [row[1] for row in await (
            await self.db.execute("PRAGMA table_info(fills)")).fetchall()]
        if "action" not in columns:
            await self.db.execute("ALTER TABLE fills ADD COLUMN action TEXT NOT NULL DEFAULT 'buy'")
        if "fee_original" not in columns:
            await self.db.execute("ALTER TABLE fills ADD COLUMN fee_original REAL")
        if "execution_legs" not in columns:
            await self.db.execute("ALTER TABLE fills ADD COLUMN execution_legs TEXT")
        feature_columns = {row[1] for row in await (
            await self.db.execute("PRAGMA table_info(features)")).fetchall()}
        additions = {
            "p_terminal":"REAL","p_settlement":"REAL","uncertainty":"REAL",
            "confidence":"REAL","regime":"TEXT","microprice":"REAL",
            "weighted_imbalance":"REAL","book_flow":"REAL","trade_flow":"REAL",
            "expected_settlement":"REAL","settlement_std":"REAL","model_source":"TEXT",
            "data_fresh":"INTEGER","target_valid":"INTEGER",
            "p_trend":"REAL","trend_strength":"REAL","horizon_agreement":"REAL",
        }
        for name,kind in additions.items():
            if name not in feature_columns:
                await self.db.execute(f"ALTER TABLE features ADD COLUMN {name} {kind}")
        await self.db.execute("""DELETE FROM closed_trades AS duplicate
            WHERE duplicate.close_type='legacy' AND EXISTS(
              SELECT 1 FROM closed_trades original
              WHERE original.id!=duplicate.id AND original.close_type!='legacy'
                AND original.experiment=duplicate.experiment
                AND original.market_ticker=duplicate.market_ticker
                AND original.side=duplicate.side)""")
        await self.db.execute("""INSERT OR IGNORE INTO closed_trades(
            experiment,market_ticker,side,contracts,entry_notional,exit_notional,pnl,fees,
            close_ts,close_type,result,source_key)
            SELECT experiment,market_ticker,side,contracts,cost,0,settled_pnl,fees,
            COALESCE((SELECT close_ts FROM markets WHERE ticker=market_ticker),0),
            'legacy',result,'legacy:'||experiment||':'||market_ticker||':'||side
            FROM positions p WHERE result IS NOT NULL AND NOT EXISTS(
              SELECT 1 FROM closed_trades c WHERE c.experiment=p.experiment
                AND c.market_ticker=p.market_ticker AND c.side=p.side)""")
        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.commit()
            await self.db.close()

    async def execute(self, sql: str, args: tuple = ()) -> None:
        assert self.db
        await self.db.execute(sql, args)
        if self.autocommit:
            await self.db.commit()

    async def raw(self, source: str, typ: str, ticker: str | None, source_ts: float | None,
                  received_ts: float, seq: int | None, payload: dict) -> None:
        await self.execute(
            "INSERT INTO raw_events(source,type,market_ticker,source_ts,received_ts,seq,payload) VALUES(?,?,?,?,?,?,?)",
            (source, typ, ticker, source_ts, received_ts, seq, json.dumps(payload, separators=(",", ":"))))

    async def market(self, m: Any) -> None:
        await self.execute("""INSERT OR REPLACE INTO markets VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (m.ticker, m.event_ticker, m.target, m.open_ts, m.close_ts, m.condition, m.status,
             m.result, m.expiration_value, json.dumps(m.raw)))

    async def rows(self, sql: str, args: tuple = ()) -> list[dict]:
        assert self.db
        self.db.row_factory = aiosqlite.Row
        async with self.db.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = await self.rows(sql, args)
        return rows[0] if rows else None
