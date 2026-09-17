from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

from .domain import iso_ts,taker_fee,taker_fee_for_legs


def repair(path: Path) -> dict:
    backup = path.with_suffix(path.suffix + ".pre-fee-fix.bak")
    source = sqlite3.connect(path)
    with sqlite3.connect(backup) as target:
        source.backup(target)
    source.row_factory = sqlite3.Row
    db = source
    db.execute("ALTER TABLE fills ADD COLUMN fee_original REAL") if "fee_original" not in {
        row[1] for row in db.execute("PRAGMA table_info(fills)")} else None
    db.execute("""CREATE TABLE IF NOT EXISTS repair_audit(
        id INTEGER PRIMARY KEY,repaired_ts REAL NOT NULL,description TEXT NOT NULL,details TEXT NOT NULL)""")

    config = {row["key"]: json.loads(row["value"])
              for row in db.execute("SELECT key,value FROM session_config")}
    bankroll = float(config.get("bankroll", 20))
    bank_rate = float(config.get("profit_bank_rate", .20))
    base_rate = float(config.get("fee_base_rate", .07))
    multiplier = float(config.get("fee_multiplier", 1.0))
    precision = float(config.get("balance_precision", .01))

    fills = [dict(row) for row in db.execute("SELECT * FROM fills ORDER BY fill_ts,id")]
    markets = {}
    for row in db.execute("SELECT * FROM markets"):
        metadata = json.loads(row["metadata"] or "{}")
        markets[row["ticker"]] = {
            "result": row["result"], "close_ts": row["close_ts"],
            "settlement_ts": iso_ts(metadata.get("settlement_ts"))}
    agents = {row["experiment"] for row in db.execute("SELECT experiment FROM accounts")}
    agents |= {row["experiment"] for row in fills}
    cash = {agent: bankroll for agent in agents}
    bank = {agent: 0.0 for agent in agents}
    realized = {agent: 0.0 for agent in agents}
    positions: dict[tuple[str,str,str], dict[str,float]] = {}
    closed: list[dict] = []
    market_realized: dict[str,dict[str,float]] = defaultdict(lambda: defaultdict(float))
    anomalies: list[str] = []
    old_fees = sum(float(row["fee"]) for row in fills)
    new_fees = 0.0

    last_fill_by_market: dict[str,float] = defaultdict(float)
    for row in fills:
        last_fill_by_market[row["market_ticker"]] = max(
            last_fill_by_market[row["market_ticker"]],row["fill_ts"])
    events = [(row["fill_ts"],0,"fill",row) for row in fills]
    for ticker, market in markets.items():
        if market["result"] in ("yes","no"):
            settlement_ts = max(market["settlement_ts"] or 0,
                                market["close_ts"]+.001,
                                last_fill_by_market[ticker]+.001)
            events.append((settlement_ts,1,"settle",(ticker,market["result"])))
    events.sort(key=lambda event:(event[0],event[1]))

    def close(agent: str, ticker: str, side: str, contracts: float, entry: float,
              proceeds: float, fees: float, pnl: float, ts: float,
              close_type: str, result: str | None = None) -> None:
        realized[agent] += pnl
        market_realized[agent][ticker] += pnl
        transfer = max(0.0,pnl)*bank_rate
        cash[agent] -= transfer
        bank[agent] += transfer
        closed.append(dict(experiment=agent,market_ticker=ticker,side=side,
            contracts=contracts,entry_notional=entry,exit_notional=proceeds,pnl=pnl,
            fees=fees,close_ts=ts,close_type=close_type,result=result))

    corrected_fees: dict[int,float] = {}
    for ts,_,kind,payload in events:
        if kind == "fill":
            row = payload
            action = (row.get("action") or "buy").lower()
            legs=json.loads(row.get("execution_legs") or "null")
            fee=(taker_fee_for_legs(legs,base_rate,multiplier,action,precision)
                 if legs else taker_fee(row["price"],row["contracts"],base_rate,multiplier,
                                        action,precision))
            corrected_fees[row["id"]] = fee
            new_fees += fee
            key = (row["experiment"],row["market_ticker"],row["side"])
            if action == "buy":
                cash[row["experiment"]] -= row["notional"]+fee
                pos = positions.setdefault(key,{"contracts":0.0,"cost":0.0,"fees":0.0})
                pos["contracts"] += row["contracts"]
                pos["cost"] += row["notional"]
                pos["fees"] += fee
            else:
                pos = positions.get(key)
                if not pos or pos["contracts"] <= 0:
                    anomalies.append(f"sell without position: fill {row['id']}")
                    continue
                filled = min(row["contracts"],pos["contracts"])
                fraction = filled/pos["contracts"]
                entry,entry_fees = pos["cost"]*fraction,pos["fees"]*fraction
                proceeds = row["notional"]*(filled/row["contracts"])
                exit_fee = fee*(filled/row["contracts"])
                pnl = proceeds-exit_fee-entry-entry_fees
                cash[row["experiment"]] += proceeds-exit_fee
                close(row["experiment"],row["market_ticker"],row["side"],filled,
                      entry,proceeds,entry_fees+exit_fee,pnl,ts,"exit")
                for field in ("contracts","cost","fees"):
                    pos[field] -= {"contracts":filled,"cost":entry,"fees":entry_fees}[field]
                if pos["contracts"] <= 1e-9:
                    positions.pop(key,None)
        else:
            ticker,result = payload
            for key,pos in list(positions.items()):
                agent,position_ticker,side = key
                if position_ticker != ticker:
                    continue
                payout = pos["contracts"] if side == result else 0.0
                pnl = payout-pos["cost"]-pos["fees"]
                cash[agent] += payout
                close(agent,ticker,side,pos["contracts"],pos["cost"],payout,
                      pos["fees"],pnl,ts,"settlement",result)
                positions.pop(key,None)

    repaired_at = time.time()
    with db:
        for row in fills:
            db.execute("""UPDATE fills SET fee_original=COALESCE(fee_original,fee),fee=?
                WHERE id=?""",(corrected_fees[row["id"]],row["id"]))
        db.execute("DELETE FROM closed_trades")
        for index,row in enumerate(closed):
            db.execute("""INSERT INTO closed_trades(experiment,market_ticker,side,contracts,
                entry_notional,exit_notional,pnl,fees,close_ts,close_type,result,source_key)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*row.values(),f"fee-repair:{index}"))
        db.execute("DELETE FROM positions")
        for (agent,ticker,side),pos in positions.items():
            db.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,0,NULL)",
                       (agent,ticker,side,pos["contracts"],pos["cost"],pos["fees"]))
        for agent in agents:
            db.execute("INSERT OR REPLACE INTO accounts VALUES(?,?,?,?,?)",
                       (agent,cash[agent],bank[agent],realized[agent],repaired_at))
            row = db.execute("SELECT state FROM agent_state WHERE experiment=?",(agent,)).fetchone()
            state = json.loads(row["state"]) if row else {}
            state["market_realized"] = dict(market_realized[agent])
            ordered = [trade for trade in closed if trade["experiment"] == agent]
            wins = losses = 0
            for trade in ordered:
                if trade["pnl"] > 0:
                    wins,losses = wins+1,0
                elif trade["pnl"] < 0:
                    wins,losses = 0,losses+1
            state["consecutive_wins"],state["consecutive_losses"] = wins,losses
            db.execute("INSERT OR REPLACE INTO agent_state VALUES(?,?,?)",
                       (agent,json.dumps(state,separators=(",",":")),repaired_at))
        db.execute("DELETE FROM equity")
        for agent in agents:
            db.execute("INSERT INTO equity(experiment,ts,cash,equity,realized) VALUES(?,?,?,?,?)",
                       (agent,repaired_at,cash[agent],cash[agent],realized[agent]))
        details = dict(backup=str(backup),fills=len(fills),closed_trades=len(closed),
            open_positions=len(positions),old_fees=old_fees,new_fees=new_fees,
            fee_savings=old_fees-new_fees,accounts={
                agent:{"cash":cash[agent],"bank":bank[agent],"total":cash[agent]+bank[agent],
                       "realized":realized[agent]} for agent in sorted(agents)},
            anomalies=anomalies)
        db.execute("INSERT INTO repair_audit(repaired_ts,description,details) VALUES(?,?,?)",
                   (repaired_at,"Correct Kalshi quadratic fee coefficient and rebuild ledger",
                    json.dumps(details,separators=(",",":"))))
        db.execute("INSERT OR REPLACE INTO session_config VALUES('fee_base_rate',?)",
                   (json.dumps(base_rate),))
        db.execute("INSERT OR REPLACE INTO session_config VALUES('balance_precision',?)",
                   (json.dumps(precision),))
    db.close()
    return details


def main() -> None:
    parser = argparse.ArgumentParser(description="Auditably repair the legacy paper ledger")
    parser.add_argument("database",type=Path)
    args = parser.parse_args()
    print(json.dumps(repair(args.database),indent=2))


if __name__ == "__main__":
    main()
