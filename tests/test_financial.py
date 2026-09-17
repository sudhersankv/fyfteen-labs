import asyncio
import json
import time

import pytest

from btc15.config import Settings
from btc15.domain import (Market,OrderBook,PriceHistory,fair_probability,taker_fee,
                          taker_fee_for_legs)
from btc15.catalog import classify_opportunities
from btc15.engine import Engine
from btc15.fleet import Fleet, compile_local
from btc15.fyften_keys import llm_config, stt_config
from btc15.names import resolve_agent_name
from btc15.voice import transcribe_audio
from btc15.model import MicrostructureState,PlattCalibrator,settlement_probability
from btc15.storage import Store
from btc15.replay import run as replay_run


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


def test_opportunity_buckets_need_window_agreement_and_leftover_edge():
    closed = classify_opportunities(
        seconds_left=500,fresh=True,yes_ask=.40,no_ask=.62,
        p_settlement=.70,p_trend=.72,fee_yes=.01,fee_no=.01,safety=.01,uncertainty=.01)
    assert {row["id"]:row["status"] for row in closed}["settlement"] == "closed"
    assert "last 3 minutes" in {row["id"]:row["plain_status"] for row in closed}["settlement"]
    open_yes = classify_opportunities(
        seconds_left=90,fresh=True,yes_ask=.40,no_ask=.62,
        p_settlement=.80,p_trend=.78,fee_yes=.01,fee_no=.01,safety=.01,uncertainty=.01)
    by_id = {row["id"]:row for row in open_yes}
    assert by_id["settlement"]["status"] == "open" and by_id["settlement"]["side"] == "yes"
    assert by_id["hybrid"]["status"] == "open"
    disagreed = classify_opportunities(
        seconds_left=90,fresh=True,yes_ask=.40,no_ask=.62,
        p_settlement=.80,p_trend=.20,fee_yes=.01,fee_no=.01,safety=.01,uncertainty=.01)
    assert {row["id"]:row["status"] for row in disagreed}["hybrid"] == "disagreement"


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
    await engine.create_agent("test-agent","settlement",20,.03,True)
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
    await engine.create_agent("test-agent","settlement",20,.03,True)
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
    await engine.create_agent("fair-value","settlement",20,.03,True)
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
    assert engine.experiments["fair-value"].policy["min_confidence"]==.50
    await engine.control_agent("fair-value","deploy")
    catalog=engine.catalog()
    assert any(k["kind"]=="settlement" for k in catalog["kinds"])
    assert {template["id"] for template in catalog["templates"]} == {
        "settlement-careful","trend-confirmed","hybrid-balanced"}
    assert catalog["compiler_contract"]["controller"] == "FYFTEN"
    assert catalog["compiler_contract"]["required"] == [
        "template_id","name","budget","deployed"]
    assert [bucket["id"] for bucket in catalog["opportunity_buckets"]] == [
        "settlement","trend","hybrid"]
    assert catalog["templates"][0]["bucket"] == "settlement"
    assert catalog["opportunity_formula"].startswith("leftover")
    assert catalog["not_opportunities"]
    dossier=await engine.agent_dossier("fair-value")
    assert dossier["guide"]["title"]=="Late closer"
    assert dossier["triggers"]["buy"] and dossier["knobs"]
    assert dossier["policy"]["entry_stop_seconds"]==60
    assert engine.compound_size(20)==4
    assert engine.compound_size(80)==16
    assert engine.compound_size(200)==20
    assert engine.smart_size(20,.70,.50,.80,.10)==1.28
    assert engine.smart_size(20,.70,.50,.80,-.01)==0
    now=time.time()
    market=Market("T","E",100,now-10,now+100,"above")
    engine.markets["T"],engine.current=market,"T"
    book=OrderBook()
    book.snapshot({"yes_dollars_fp":[[".70","10"]],"no_dollars_fp":[[".75","10"]]},1)
    book.updated=now
    engine.books["T"]=book
    exp=engine.experiments["fair-value"]
    assert engine.risk_reason(exp,market,book,
        {"confidence":1,"seconds_left":100,"spread":.04},now,False,"yes",.1)=="BLOCKED: spread"
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


def test_resolve_boomer_from_spoken_phrase():
    assert resolve_agent_name("go to my boomer agent from the roster",
                              ["plsmakemoney","boomer","agromonster"]) == "boomer"
    assert compile_local("how many agents are deployed right now", ["boomer"])["action"] == "list_roster"
    assert compile_local("give him $20 more", ["boomer"], "boomer") == {
        "action":"add_cash","name":"boomer","dollars":20.0}
    assert compile_local("I think he's too passive", ["boomer"], "boomer")["direction"] == "more"
    assert compile_local("cash out and retire this person", ["boomer"], "boomer")["action"] == "retire_agent"
    assert compile_local("make him a direction bot", ["boomer"], "boomer")["kind"] == "trend-rider"


@pytest.mark.asyncio
async def test_speech_backend_requires_a_key(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ("FYFTEN_STT_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY",
                 "FYFTEN_LLM_API_KEY", "FIREWORKS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert not stt_config()["ready"]
    with pytest.raises(ValueError, match="No speech key"):
        await transcribe_audio(b"RIFF....", "audio/wav")


def test_fyften_defaults_fireworks_glm_and_groq_whisper(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ("FYFTEN_LLM_API_KEY", "FIREWORKS_API_KEY", "GROQ_API_KEY",
                 "FYFTEN_STT_API_KEY", "FYFTEN_LLM_BASE_URL", "FYFTEN_LLM_MODEL",
                 "FYFTEN_STT_PROVIDER", "FYFTEN_STT_URL", "FYFTEN_STT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    llm = llm_config()
    assert llm["base"] == "https://api.fireworks.ai/inference/v1"
    assert llm["model"] == "accounts/fireworks/models/glm-5p3-flash"
    stt = stt_config()
    assert stt["provider"] == "groq"
    assert stt["model"] == "whisper-large-v3-turbo"
    assert "groq.com" in stt["url"]


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
async def test_fyfteen_fleet_inspects_funds_and_retires(tmp_path):
    store = Store(tmp_path/"fleet.db"); await store.open()
    engine = Engine(Settings(db_path=tmp_path/"fleet.db", latency_ms=0, min_valid_btc_target=1), store)
    engine.fees_verified = True
    await engine.create_agent("boomer","settlement",20,.03,True)
    fleet = Fleet(engine)
    look = await fleet.chat("go to my boomer agent from the roster")
    assert look["ok"] and look["focus"] == "boomer" and "20.00" in look["reply"]
    switched = await fleet.chat("make him a direction bot")
    assert switched["ok"] and engine.experiments["boomer"].kind == "trend-rider"
    edge = await fleet.chat("what's his current edge")
    assert "leftover" in edge["reply"].lower() or "Required leftover" in edge["reply"]
    funded = await fleet.chat("give him $20 more")
    assert funded["ok"] and engine.experiments["boomer"].cash == 40
    assert engine.experiments["boomer"].allocated_capital == 40
    roster = await fleet.chat("how many agents are deployed right now")
    assert "1 deployed" in roster["reply"]
    retired = await fleet.chat("cash out and retire this person")
    assert retired["ok"] and "boomer" not in engine.experiments
    await store.close()
