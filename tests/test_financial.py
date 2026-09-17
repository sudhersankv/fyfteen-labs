import asyncio
import json
import time

import pytest

from btc15.config import Settings
from btc15.domain import (Market,OrderBook,PriceHistory,fair_probability,taker_fee,
                          taker_fee_for_legs)
from btc15.catalog import classify_opportunities, leftover_edge
from btc15.engine import Engine
from btc15.fleet import TOOLS, Fleet, compile_local
from btc15.fyften_keys import llm_config
from btc15.names import resolve_agent_name
from btc15.model import MicrostructureState,PlattCalibrator,settlement_probability
from btc15.storage import Store
from btc15.replay import run as replay_run
from btc15 import auth


def test_unified_yes_price_book_and_executable_prices():
    book = OrderBook()
    book.snapshot({"yes_dollars_fp": [["0.46", "10"], ["0.45", "5"]],
                   "no_dollars_fp": [["0.49", "4"], ["0.51", "8"]]}, 10)
    assert book.yes_bid == .46
    assert book.yes_ask == .49
    assert book.no_bid == .51
    assert book.no_ask == .54
    filled, cost = book.walk_buy("yes", 6)
    assert filled == 6 and cost == pytest.approx(4*.49 + 2*.51)
    filled, cost = book.walk_buy("no", 12)
    assert filled == 12 and cost == pytest.approx(10*.54 + 2*.55)
    filled, proceeds = book.walk_sell("yes", 6)
    assert filled == 6 and proceeds == pytest.approx(6*.46)
    filled, proceeds = book.walk_sell("no", 6)
    assert filled == 6 and proceeds == pytest.approx(4*.51 + 2*.49)


def test_orderbook_delta_and_deletion_sequence_is_checked_by_subscription():
    book = OrderBook()
    book.snapshot({"yes_dollars_fp": [["0.4", "3"]], "no_dollars_fp": []}, 3)
    assert book.delta({"side": "yes", "price_dollars": ".4", "delta_fp": "-3"}, 4)
    assert book.yes_bid is None
    assert book.delta({"side": "yes", "price_dollars": ".4", "delta_fp": "1"}, 6)
    assert book.valid


@pytest.mark.asyncio
async def test_subscription_sequence_allows_interleaved_markets_and_rejects_real_gap(tmp_path):
    store = Store(tmp_path/"seq.db"); await store.open()
    engine = Engine(Settings(db_path=tmp_path/"seq.db"), store)
    now = time.time()
    for ticker in ("CURRENT", "NEXT"):
        engine.books[ticker] = OrderBook()
    await engine.on_event({"type":"orderbook_snapshot","sid":7,"seq":1,
        "msg":{"market_ticker":"CURRENT","yes_dollars_fp":[[".4","3"]],"no_dollars_fp":[[".5","3"]]}}, now)
    await engine.on_event({"type":"orderbook_snapshot","sid":7,"seq":2,
        "msg":{"market_ticker":"NEXT","yes_dollars_fp":[[".3","2"]],"no_dollars_fp":[[".6","2"]]}}, now+.01)
    await engine.on_event({"type":"orderbook_delta","sid":7,"seq":3,
        "msg":{"market_ticker":"CURRENT","side":"yes","price_dollars":".4","delta_fp":"1"}}, now+.02)
    assert engine.books["CURRENT"].yes[.4] == 4
    with pytest.raises(RuntimeError, match="expected 4, got 5"):
        await engine.on_event({"type":"orderbook_delta","sid":7,"seq":5,
            "msg":{"market_ticker":"NEXT","side":"yes","price_dollars":".3","delta_fp":"1"}}, now+.03)
    assert not engine.books["CURRENT"].valid and not engine.books["NEXT"].valid
    await engine.on_event({"type":"orderbook_snapshot","sid":7,"seq":1,
        "msg":{"market_ticker":"CURRENT","yes_dollars_fp":[[".4","2"]],
               "no_dollars_fp":[[".5","2"]]}},now+.04)
    assert engine.books["CURRENT"].valid and engine.orderbook_seq==1
    await store.close()


def test_target_extraction_and_times():
    m = Market.from_api({"ticker": "X", "event_ticker": "E", "floor_strike": "104250.50",
                         "open_time": "2026-09-16T07:00:00Z",
                         "close_time": "2026-09-16T07:15:00Z", "strike_type": "greater",
                         "rules_primary": "BRTI above the strike"})
    assert m.target == 104250.5
    assert m.close_ts - m.open_ts == 900
    fallback = Market.from_api({"ticker":"Y","title":"BTC above $105,000?","open_time":1,"close_time":2})
    assert fallback.target == 105000
    invalid = Market.from_api({"ticker":"Z","title":"BTC 15 minute market","open_time":1,"close_time":2})
    assert invalid.target == 0


def test_probability_direction_volatility_and_time():
    assert fair_probability(101, 100, 30, .001) > .5
    assert fair_probability(99, 100, 30, .001) < .5
    assert fair_probability(101, 100, 30, .005) < fair_probability(101, 100, 30, .001)


def test_settlement_probability_models_known_and_remaining_observations():
    low,expected,std=settlement_probability(100,100.5,30,.001,100,30)
    high,high_expected,_=settlement_probability(100,100.5,30,.001,102,30)
    assert expected==pytest.approx(100)
    assert high_expected==pytest.approx(101)
    assert high>low and std>0
    locked,locked_average,locked_std=settlement_probability(100,100.5,0,.001,100.6,60)
    assert locked==.995 and locked_average==100.6 and locked_std==0


def test_settlement_uncertainty_includes_wait_before_final_minute():
    _,_,far_std=settlement_probability(100,100,600,.001)
    _,_,near_std=settlement_probability(100,100,60,.001)
    assert far_std>near_std


def test_opportunity_windows_follow_the_three_templates():
    closed = classify_opportunities(
        seconds_left=500,fresh=True,yes_ask=.40,no_ask=.62,
        p_settlement=.70,p_trend=.72,p_terminal=.71,
        fee_yes=.01,fee_no=.01,safety=.01,uncertainty=.01)
    by_id = {row["id"]:row for row in closed}
    assert set(by_id) == {"fair-value", "momentum", "late-settlement"}
    assert by_id["late-settlement"]["status"] == "closed"
    assert "3 minutes" in by_id["late-settlement"]["plain_status"]
    assert by_id["fair-value"]["status"] == "open"
    open_yes = classify_opportunities(
        seconds_left=90,fresh=True,yes_ask=.40,no_ask=.62,
        p_settlement=.80,p_trend=.78,p_terminal=.79,
        fee_yes=.01,fee_no=.01,safety=.01,uncertainty=.01)
    by_id = {row["id"]:row for row in open_yes}
    assert by_id["late-settlement"]["status"] == "open" and by_id["late-settlement"]["side"] == "yes"
    assert by_id["momentum"]["status"] == "open"
    stale = classify_opportunities(
        seconds_left=90,fresh=False,yes_ask=.40,no_ask=.62,
        p_settlement=.80,p_trend=.20,p_terminal=.50,
        fee_yes=.01,fee_no=.01,safety=.01,uncertainty=.01)
    assert all(row["status"] == "stale" for row in stale)


def test_shrunk_drift_moves_forecast_without_becoming_deterministic():
    flat,_,_=settlement_probability(100,100,300,.001,drift_per_second=0)
    rising,_,_=settlement_probability(100,100,300,.001,drift_per_second=.00002)
    falling,_,_=settlement_probability(100,100,300,.001,drift_per_second=-.00002)
    assert falling < flat < rising
    assert .05 < falling < rising < .95


def test_microstructure_uses_depth_shape_microprice_and_event_direction():
    book=OrderBook()
    book.snapshot({"yes_dollars_fp":[[".49","12"],[".48","3"]],
                   "no_dollars_fp":[[".51","4"],[".52","10"]]},1)
    assert book.microprice==pytest.approx((.51*12+.49*4)/16)
    assert book.weighted_imbalance>0
    flow=MicrostructureState()
    flow.reset(100)
    flow.on_delta("yes",10,101)
    assert flow.book_flow>0
    flow.on_trade("no",10,102)
    assert flow.trade_flow<0


def test_calibrator_stays_disabled_without_professional_sample_minimum():
    calibrator=PlattCalibrator(min_samples=200)
    calibrator.fit([(.7,1),(.3,0)]*20)
    assert not calibrator.active
    assert calibrator.status()["samples"]==40


def test_fee_rounds_up_and_settlement_pnl():
    assert taker_fee(.5, 1, .07) == .02
    assert taker_fee(.7, 10, .07) == .15
    assert taker_fee(.5, 100, .07, 1.0) == pytest.approx(1.75)
    assert taker_fee(.5, 100, .07, .5) == pytest.approx(.88)
    assert taker_fee(.3301, .03, .07, 1.0, "buy", .01) == pytest.approx(.010097)
    assert taker_fee(.3301, .03, .07, 1.0, "buy", .0001) == pytest.approx(.000597)
    gross = 3 - 3*.60
    assert gross - taker_fee(.6, 3, .07) == pytest.approx(1.14)


def test_multi_level_execution_fees_use_each_observed_price():
    legs=[(.10,100),(.90,100)]
    assert taker_fee_for_legs(legs,.07,1,"buy",.0001)==pytest.approx(1.26)
    assert taker_fee(.50,200,.07,1,"buy",.0001)==pytest.approx(3.50)


def test_history_has_no_future_information():
    h = PriceHistory()
    h.add(100, 100)
    h.add(101, 101)
    h.add(102, 1000)  # future relative to evaluation time
    assert h.at(1, 101) == 100


@pytest.mark.asyncio
async def test_brti_final_minute_fields_and_latency_fill(tmp_path):
    store = Store(tmp_path / "x.db")
    await store.open()
    s = Settings(db_path=tmp_path/"x.db", latency_ms=5, min_liquidity=1,
                 cooldown_seconds=0, min_seconds_remaining=0, max_spread=.5,
                 min_valid_btc_target=1)
    engine = Engine(s, store)
    engine.fees_verified = True
    await engine.create_agent("test-agent","late-settlement",20,.03,True)
    now = time.time()
    m = Market("T","E",100,now-10,now+100,"above")
    engine.markets["T"], engine.current = m, "T"
    engine.books["T"] = OrderBook()
    engine.books["T"].snapshot({"yes_dollars_fp":[[".4","20"]],"no_dollars_fp":[[".5","20"]]},1)
    engine.books["T"].updated = now
    engine.brti = 101
    for i in range(120):
        engine.fast_history.add(now-60+i*.5,100.8+i*.2/119)
    event = {"type":"cfbenchmarks_value","seq":1,"msg":{"received_at":int(now*1000),
        "data":json.dumps({"time":int(now*1000),"value":"101"}),
        "avg_60s_data":{"value":"100","window_size":59},
        "last_60s_windowed_average_15min":{"value":"100.75","window_size":42}}}
    await engine.on_event(event, now)
    assert engine.settlement_avg == 100.75 and engine.settlement_count == 42
    assert engine.brti_received_ts == now
    await engine.evaluate(now)
    await engine.evaluate(now)
    await asyncio.sleep(.03)
    fills = await store.rows("SELECT * FROM fills")
    assert fills and all(x["fill_ts"] >= x["decision_ts"] for x in fills)
    assert all(x["price"] == .5 for x in fills if x["side"] == "yes")
    await store.close()


@pytest.mark.asyncio
async def test_risk_blocks_stale_and_settlement_uses_official_result(tmp_path):
    store = Store(tmp_path/"x.db"); await store.open()
    engine = Engine(Settings(db_path=tmp_path/"x.db"), store)
    await engine.create_agent("test-agent","late-settlement",20,.03,True)
    now = time.time()
    m = Market("T","E",100,now-10,now+10,"above")
    engine.markets["T"], engine.current = m, "T"
    b = OrderBook(); b.snapshot({"yes_dollars_fp":[[".4","5"]],"no_dollars_fp":[[".5","5"]]},1)
    engine.books["T"] = b
    x = {"seconds_left":9,"spread":.1}
    assert "stale" in engine.risk_reason(engine.experiments["test-agent"],m,b,x,now,True,"yes",.1)
    await store.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,0,NULL)",("fair-2","T","yes",2,1.2,.1))
    await engine.settle("T","yes")
    pos = await store.one("SELECT * FROM positions WHERE experiment='fair-2'")
    assert pos["settled_pnl"] == pytest.approx(.7)
    await store.close()


@pytest.mark.asyncio
async def test_rollover_selects_current_and_next_not_expired(tmp_path):
    store = Store(tmp_path/"x.db"); await store.open()
    engine = Engine(Settings(db_path=tmp_path/"x.db"), store)
    now = time.time()
    def raw(ticker, start):
        return {"ticker":ticker,"event_ticker":"E","floor_strike":"100000",
                "open_time":start,"close_time":start+900,"rules_primary":"above","status":"active"}
    async def markets(status):
        return [raw("OLD",now-1000),raw("CURRENT",now-10),raw("NEXT",now+890)] if status=="open" else []
    engine.client.markets = markets
    engine.settlement_avg,engine.settlement_count=99,60
    await engine.discover()
    assert engine.current == "CURRENT"
    assert engine.active_tickers() == ["CURRENT","NEXT"]
    assert engine.settlement_avg is None and engine.settlement_count==0
    await store.close()


@pytest.mark.asyncio
async def test_replay_preserves_receive_order(tmp_path):
    source = Store(tmp_path/"source.db"); await source.open()
    now = time.time()
    market = Market("T","E",100,now-1,now+900,"above",
                    raw={"ticker":"T","event_ticker":"E","floor_strike":"100",
                         "open_time":now-1,"close_time":now+900,"rules_primary":"above"})
    await source.market(market)
    await source.raw("kalshi","ticker","T",now,now,1,{"type":"ticker","msg":{"market_ticker":"T","n":1}})
    await source.raw("kalshi","ticker","T",now,now,2,{"type":"ticker","msg":{"market_ticker":"T","n":2}})
    await source.close()
    await replay_run(tmp_path/"source.db",tmp_path/"out.db",10_000)
    out = Store(tmp_path/"out.db"); await out.open()
    rows = await out.rows("SELECT payload FROM raw_events ORDER BY id")
    assert [json.loads(x["payload"])["msg"]["n"] for x in rows] == [1,2]
    await out.close()


@pytest.mark.asyncio
async def test_dynamic_agent_and_profitable_exit_banks_twenty_percent(tmp_path):
    store = Store(tmp_path/"agents.db"); await store.open()
    settings = Settings(db_path=tmp_path/"agents.db",latency_ms=0,profit_bank_rate=.20,
                        dollars_per_trade=20,capital_fraction_per_trade=.20,
                        min_valid_btc_target=1)
    engine = Engine(settings,store)
    engine.fees_verified=True
    assert not engine.experiments
    await engine.create_agent("fair-value","fair-value",20,.03,True)
    assert list(engine.experiments)==["fair-value"]
    assert engine.experiments["fair-value"].deployed
    await engine.control_agent("fair-value","pause")
    assert not engine.experiments["fair-value"].deployed
    await engine.control_agent("fair-value","budget",25)
    assert engine.experiments["fair-value"].cash==25
    added = await engine.control_agent("fair-value","add_cash",delta=10)
    assert engine.experiments["fair-value"].cash==35
    assert added["allocated_capital"]==35
    await engine.control_agent("fair-value","budget",20)
    await engine.control_agent("fair-value","threshold",threshold=.04)
    assert engine.experiments["fair-value"].threshold==.04
    await engine.control_agent("fair-value","policy",policy={
        "max_spread":.03,"min_confidence":.50,"entry_start_seconds":600,
        "entry_stop_seconds":60,"max_market_exposure":15,"max_total_exposure":20,
    })
    assert engine.experiments["fair-value"].policy["max_spread"]==.03
    assert "min_confidence" not in engine.experiments["fair-value"].policy
    await engine.control_agent("fair-value","deploy")
    catalog=engine.catalog()
    assert {k["kind"] for k in catalog["kinds"]} == {"fair-value","momentum","late-settlement"}
    assert {template["id"] for template in catalog["templates"]} == {
        "fair-value","momentum","late-settlement"}
    assert catalog["compiler_contract"]["controller"] == "FYFTEN"
    assert catalog["compiler_contract"]["required"] == ["template_id","name","budget"]
    assert [bucket["id"] for bucket in catalog["opportunity_buckets"]] == [
        "fair-value","momentum","late-settlement"]
    assert catalog["templates"][0]["id"] == "fair-value"
    assert "model probability" in catalog["opportunity_formula"]
    assert catalog["not_opportunities"]
    dossier=await engine.agent_dossier("fair-value")
    assert dossier["guide"]["title"]=="Fair Value"
    assert dossier["triggers"]["buy"] and dossier["knobs"]
    assert dossier["policy"]["entry_stop_seconds"]==60
    assert dossier["why_not"]["counts"] == []
    assert engine.compound_size(20)==4
    assert engine.compound_size(80)==16
    assert engine.compound_size(200)==20
    assert engine.smart_size(20,.70,.50,.80,.10)==4.0
    assert engine.smart_size(20,.70,.50,.80,-.01)==0
    now=time.time()
    market=Market("T","E",100,now-10,now+100,"above")
    engine.markets["T"],engine.current=market,"T"
    book=OrderBook()
    book.snapshot({"yes_dollars_fp":[[".70","10"]],"no_dollars_fp":[[".75","10"]]},1)
    book.updated=now
    engine.books["T"]=book
    exp=engine.experiments["fair-value"]
    assert "spread" in engine.risk_reason(exp,market,book,
        {"confidence":1,"seconds_left":100,"spread":.04},now,False,"yes",.1).lower()
    exp.cash=18.90
    exp.exposure_by_market["T"]=1.0
    await store.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,0,NULL)",
                        (exp.name,"T","yes",2,1.0,.10))
    await engine.execute_after_latency(exp,market,"sell","yes",now,contracts=2)
    assert await store.one("SELECT * FROM positions WHERE experiment=?",(exp.name,)) is None
    closed=await store.one("SELECT * FROM closed_trades WHERE experiment=?",(exp.name,))
    assert closed["close_type"]=="exit"
    assert closed["pnl"]==pytest.approx(.27)
    assert exp.bank==pytest.approx(.054)
    assert exp.cash+exp.bank==pytest.approx(20.27)
    await engine.execute_after_latency(exp,market,"buy","yes",now+.01,dollars=4)
    reopened=await store.one("SELECT * FROM positions WHERE experiment=?",(exp.name,))
    assert reopened["result"] is None
    assert reopened["contracts"] > 0
    await store.close()


@pytest.mark.asyncio
async def test_legacy_closed_trade_migration_does_not_duplicate_settlement(tmp_path):
    path=tmp_path/"ledger.db"
    store=Store(path); await store.open()
    await store.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)",
                        ("bot","T","yes",2,1.2,.1,.7,"yes"))
    await store.execute("""INSERT INTO closed_trades(experiment,market_ticker,side,contracts,
        entry_notional,exit_notional,pnl,fees,close_ts,close_type,result,source_key)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("bot","T","yes",2,1.2,2,.7,.1,time.time(),"settlement","yes","settled:T"))
    await store.execute("""INSERT INTO closed_trades(experiment,market_ticker,side,contracts,
        entry_notional,exit_notional,pnl,fees,close_ts,close_type,result,source_key)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("bot","T","yes",2,1.2,0,.7,.1,time.time(),"legacy","yes","legacy:bot:T:yes"))
    await store.close()
    reopened=Store(path); await reopened.open()
    rows=await reopened.rows("SELECT close_type FROM closed_trades")
    assert [row["close_type"] for row in rows]==["settlement"]
    await reopened.close()


def test_resolve_bot_names_and_local_compiler():
    assert resolve_agent_name("go to my boomer agent from the roster",
                              ["plsmakemoney","boomer","agromonster"]) == "boomer"
    assert compile_local("how many agents are deployed right now", ["boomer"])["action"] == "list_bots"
    assert compile_local("give him $20 more", ["boomer"], "boomer") == {
        "action":"add_cash","name":"boomer","dollars":20.0}
    assert compile_local("I think he's too passive", ["boomer"], "boomer")["direction"] == "more"
    assert compile_local("cash out and retire this person", ["boomer"], "boomer")["action"] == "retire_bot"
    assert compile_local("make him a direction bot", ["boomer"], "boomer")["kind"] == "momentum"
    created = compile_local("Create a momentum bot called spike with $100", [])
    assert created["action"] == "create_bot"
    assert created["name"] == "spike"
    assert created["template_id"] == "momentum"
    assert created["budget"] == 100
    assert compile_local("Why didn't spike trade?", ["spike"])["action"] == "why_not_traded"


def test_fyften_defaults_fireworks_glm(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ("FYFTEN_LLM_API_KEY", "FIREWORKS_API_KEY", "OPENAI_API_KEY",
                 "FYFTEN_LLM_BASE_URL", "FYFTEN_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    llm = llm_config()
    assert llm["base"] == "https://api.fireworks.ai/inference/v1"
    assert llm["model"] == "accounts/fireworks/models/glm-5p3-flash"
    assert not llm["key"]


@pytest.mark.asyncio
async def test_history_confidence_uses_brti_source_clock(tmp_path):
    store = Store(tmp_path/"clock.db"); await store.open()
    engine = Engine(Settings(db_path=tmp_path/"clock.db", min_valid_btc_target=1), store)
    now = time.time()
    engine.markets["T"] = Market("T","E",100,now-100,now+400,"above")
    engine.current = "T"
    engine.books["T"] = OrderBook()
    engine.books["T"].snapshot({"yes_dollars_fp":[[".4","20"]],"no_dollars_fp":[[".5","20"]]},1)
    engine.brti = 101
    for i in range(40):
        engine.fast_history.add(now-100+i, 100.4+i*.01)
    x = engine.features(now)
    assert x["history_confidence"] > .5
    await store.close()


@pytest.mark.asyncio
async def test_fyfteen_fleet_inspects_funds_and_retires(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("FIREWORKS_API_KEY", "FYFTEN_LLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("btc15.fleet.llm_settings",
                        lambda: {"key": "", "base": "", "model": "local-compiler"})
    store = Store(tmp_path/"fleet.db"); await store.open()
    engine = Engine(Settings(db_path=tmp_path/"fleet.db", latency_ms=0, min_valid_btc_target=1), store)
    engine.fees_verified = True
    await engine.create_agent("boomer","late-settlement",20,.03,True)
    fleet = Fleet(engine)
    look = await fleet.chat("go to my boomer agent from the roster")
    assert look["ok"] and look["focus"] == "boomer" and "20.00" in look["reply"]
    switched = await fleet.chat("make him a direction bot")
    assert switched["ok"] and not switched.get("needs_confirm")
    assert engine.experiments["boomer"].kind == "momentum"
    edge = await fleet.chat("what's his current edge")
    assert "cash" in edge["reply"].lower() or "momentum" in edge["reply"].lower()
    funded = await fleet.chat("give him $20 more")
    assert funded["ok"] and not funded.get("needs_confirm")
    assert engine.experiments["boomer"].cash == 40
    assert engine.experiments["boomer"].allocated_capital == 40
    roster = await fleet.chat("how many agents are deployed right now")
    assert "1 deployed" in roster["reply"]
    retired = await fleet.chat("cash out and retire this person")
    assert retired["ok"] and "boomer" not in engine.experiments
    missing = await fleet.chat("create a direction bot")
    assert not missing["ok"] and "name" in missing["reply"].lower()
    created = await fleet.chat("Create a momentum bot called spike with $100")
    assert created["ok"] and engine.experiments["spike"].kind == "momentum"
    assert engine.experiments["spike"].allocated_capital == 100
    await store.close()


@pytest.mark.asyncio
async def test_empty_db_seeds_three_preset_jobs(tmp_path):
    store = Store(tmp_path/"seed.db"); await store.open()
    engine = Engine(Settings(db_path=tmp_path/"seed.db", bankroll=20, min_valid_btc_target=1), store)
    await engine.restore_agents()
    assert set(engine.experiments) == {"fair-value", "momentum", "late-settlement"}
    assert engine.experiments["fair-value"].kind == "fair-value"
    assert engine.experiments["momentum"].kind == "momentum"
    assert engine.experiments["late-settlement"].kind == "late-settlement"
    assert all(exp.cash == 20 for exp in engine.experiments.values())
    await store.close()


@pytest.mark.asyncio
async def test_snapshot_and_dossier_show_open_holdings(tmp_path):
    store = Store(tmp_path/"own.db"); await store.open()
    engine = Engine(Settings(db_path=tmp_path/"own.db", min_valid_btc_target=1), store)
    await engine.create_agent("direction","momentum",20,.03,True)
    now = time.time()
    engine.markets["NOW"] = Market("NOW","E",100,now-10,now+100,"above")
    engine.current = "NOW"
    engine.books["NOW"] = OrderBook()
    engine.books["NOW"].snapshot({"yes_dollars_fp":[[".4","20"]],"no_dollars_fp":[[".5","20"]]},1)
    engine.brti = 101
    await store.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,0,NULL)",
                        ("direction","OLD","yes",2,0.80,0.04))
    await store.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,0,NULL)",
                        ("direction","NOW","no",1,0.45,0.02))
    snap = await engine.snapshot()
    row = next(s for s in snap["strategies"] if s["name"]=="direction")
    assert {"YES 2.00", "NO 1.00"} <= set(row["position"].split(" + "))
    assert {h["market_ticker"] for h in row["holdings"]} == {"OLD","NOW"}
    dossier = await engine.agent_dossier("direction")
    assert set(dossier["position"].split(" + ")) == set(row["position"].split(" + "))
    assert len(dossier["holdings"]) == 2
    await store.close()


def test_leftover_edge_is_probability_minus_ask_fee_and_safety():
    assert leftover_edge(.72, .64, .018, .01) == pytest.approx(.052)
    assert leftover_edge(None, .64, .018, .01) is None
    assert leftover_edge(.5, .5, .02, .01) == pytest.approx(-.03)


def _book(yes_bid=.40, yes_ask=.50, size=20):
    book = OrderBook()
    book.snapshot({"yes_dollars_fp":[[str(yes_bid), str(size)]],
                   "no_dollars_fp":[[str(yes_ask), str(size)]]}, 1)
    return book


async def _ready_engine(tmp_path, kind="fair-value", threshold=.03):
    store = Store(tmp_path/"bots.db")
    await store.open()
    engine = Engine(Settings(
        db_path=tmp_path/"bots.db", latency_ms=0, min_valid_btc_target=1,
        cooldown_seconds=0, min_seconds_remaining=5, max_spread=.20, min_liquidity=1,
        dollars_per_trade=10), store)
    engine.fees_verified = True
    await engine.create_agent("bot", kind, 100, threshold, True)
    now = time.time()
    market = Market("T","E",100,now-10,now+400,"above")
    engine.markets["T"], engine.current = market, "T"
    book = _book()
    book.updated = now
    engine.books["T"] = book
    return engine, store, market, book, now


def _x(seconds_left=400, p_terminal=.80, p_trend=.80, p_settlement=.80, spread=.10):
    return {"seconds_left": seconds_left, "spread": spread, "p_terminal": p_terminal,
            "p_yes": p_terminal, "p_trend": p_trend, "p_settlement": p_settlement}


@pytest.mark.asyncio
async def test_fair_value_buys_when_leftover_clears_and_holds_when_it_does_not(tmp_path):
    engine, store, market, book, now = await _ready_engine(tmp_path, "fair-value", .04)
    exp = engine.experiments["bot"]
    await engine.decide(exp, market, book, _x(p_terminal=.80), now, False)
    assert exp.current_action == "BUY_YES"
    assert "Model probability 80%" in exp.reason
    assert "Estimated edge after fees" in exp.reason
    await engine.decide(exp, market, book, _x(p_terminal=.51, p_trend=.51, p_settlement=.51), now, False)
    assert exp.current_action == "HOLD"
    assert "below the required 4.0%" in exp.reason
    await engine.decide(exp, market, book, _x(), now, True)
    assert exp.current_action == "WAIT"
    assert "stale" in exp.reason.lower()
    why = await engine.why_not_traded("bot", seconds=3600)
    labels = {row["code"]: row["n"] for row in why["counts"]}
    assert labels.get("stale", 0) >= 1
    assert labels.get("below-edge", 0) >= 1
    await store.close()


@pytest.mark.asyncio
async def test_momentum_tilts_fair_value_without_horizon_vetoes(tmp_path):
    engine, store, market, book, now = await _ready_engine(tmp_path, "momentum", .03)
    exp = engine.experiments["bot"]
    assert engine.agent_probability(exp, _x(p_terminal=.50, p_trend=.90)) == pytest.approx(.72)
    await engine.decide(exp, market, book, _x(p_terminal=.50, p_trend=.90), now, False)
    assert exp.current_action == "BUY_YES"
    await store.close()


@pytest.mark.asyncio
async def test_late_settlement_waits_until_the_final_three_minutes(tmp_path):
    engine, store, market, book, now = await _ready_engine(tmp_path, "late-settlement", .03)
    exp = engine.experiments["bot"]
    await engine.decide(exp, market, book, _x(seconds_left=500, p_settlement=.90), now, False)
    assert exp.current_action == "WAIT"
    assert "minutes remain" in exp.reason
    await engine.decide(exp, market, book, _x(seconds_left=90, p_settlement=.90), now, False)
    assert exp.current_action == "BUY_YES"
    await store.close()


@pytest.mark.asyncio
async def test_one_bot_exception_does_not_stop_the_others(tmp_path):
    engine, store, market, book, now = await _ready_engine(tmp_path, "fair-value")
    await engine.create_agent("other","momentum",50,.03,True)
    engine.brti = 101
    engine.brti_received_ts = now
    original = engine.decide
    async def boom(exp, *args, **kwargs):
        if exp.name == "bot":
            raise RuntimeError("strategy exploded")
        return await original(exp, *args, **kwargs)
    engine.decide = boom
    await engine.evaluate(now)
    assert engine.experiments["bot"].current_action == "ERROR"
    assert engine.experiments["other"].current_action in {"BUY_YES","BUY_NO","HOLD","WAIT"}
    await store.close()


def test_chatbot_tool_schemas_are_validated_and_closed():
    names = {item["function"]["name"] for item in TOOLS}
    assert {"list_bots","inspect_bot","create_bot","deploy_bot","pause_bot","retire_bot",
            "add_cash","set_threshold","set_beginner_settings","update_advanced_settings",
            "why_not_traded","explain_template","summarize_market"} <= names
    create = next(item["function"] for item in TOOLS if item["function"]["name"]=="create_bot")
    assert create["parameters"]["required"] == ["template_id","name","budget"]
    assert create["parameters"]["properties"]["template_id"]["enum"] == [
        "fair-value","momentum","late-settlement"]


@pytest.mark.asyncio
async def test_unknown_advanced_settings_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("btc15.fleet.llm_settings",
                        lambda: {"key": "", "base": "", "model": "local-compiler"})
    store = Store(tmp_path/"adv.db"); await store.open()
    engine = Engine(Settings(db_path=tmp_path/"adv.db", min_valid_btc_target=1), store)
    await engine.create_agent("spike","fair-value",50,.03,True)
    fleet = Fleet(engine)
    with pytest.raises(ValueError, match="Unknown settings"):
        await fleet._run({"action":"update_advanced_settings","name":"spike",
                          "kelly_fraction":.4})
    updated = await fleet._run({"action":"update_advanced_settings","name":"spike",
                                "cooldown_seconds":45})
    assert updated["ok"] and engine.experiments["spike"].policy["cooldown_seconds"]==45
    await store.close()


def test_auth_protects_reads_and_mutations(monkeypatch):
    monkeypatch.setenv("FYFTEEN_PASSWORD", "demo-pass")
    monkeypatch.setenv("FYFTEEN_USER", "fyfteen")
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    app = FastAPI()
    auth.install(app)

    @app.get("/secret")
    def secret(request: Request):
        auth.require_user(request)
        return {"ok": True}

    @app.post("/mutate")
    def mutate(request: Request):
        auth.require_user(request)
        auth.require_csrf(request)
        return {"ok": True}

    @app.post("/login")
    def login(request: Request):
        assert auth.verify_login("fyfteen", "demo-pass")
        auth.login(request, "fyfteen")
        return {"ok": True, "csrf": auth.csrf_token(request)}

    client = TestClient(app)
    assert client.get("/secret").status_code == 401
    assert client.post("/mutate").status_code == 401
    login = client.post("/login")
    assert login.status_code == 200
    csrf = login.json()["csrf"]
    assert client.get("/secret").status_code == 200
    assert client.post("/mutate").status_code == 403
    assert client.post("/mutate", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert not auth.verify_login("fyfteen", "wrong")
