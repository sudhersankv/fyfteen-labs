from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .config import Settings
from .engine import Engine
from .fleet import Fleet
from .storage import Store
from .voice import transcribe_audio

s = Settings()
store = Store(s.db_path)
engine = Engine(s, store)
fleet = Fleet(engine)
engine.fleet = fleet


class ManualPaperOrder(BaseModel):
    action: str
    side: str
    amount: float = Field(default=1.0, ge=0, le=100)


class AgentCreate(BaseModel):
    name: str = Field(min_length=2,max_length=32)
    template_id: str | None = None
    kind: str | None = None
    budget: float = Field(ge=1,le=10_000)
    threshold: float | None = Field(default=None,ge=.005,le=.20)
    deployed: bool = True
    policy: dict[str,float] | None = None


class AgentControl(BaseModel):
    action: str
    budget: float | None = Field(default=None,ge=1,le=10_000)
    threshold: float | None = Field(default=None,ge=.005,le=.20)
    policy: dict[str,float] | None = None
    delta: float | None = Field(default=None,ge=.01,le=10_000)
    flatten: bool = False
    kind: str | None = None
    template_id: str | None = None


class FleetChat(BaseModel):
    text: str = Field(min_length=0, max_length=4000)
    reset: bool = False


class VoiceClip(BaseModel):
    audio_b64: str = Field(min_length=16)
    content_type: str = "audio/wav"


@asynccontextmanager
async def lifespan(_: FastAPI):
    await store.open()
    await engine.start()
    yield
    await engine.stop()
    await store.close()


app = FastAPI(title="fyfteen labs", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    from importlib.resources import files
    return (files("btc15") / "dashboard.html").read_text(encoding="utf-8")


@app.get("/api/state")
async def state():
    return await engine.snapshot()


@app.get("/api/catalog")
async def catalog():
    return engine.catalog()


@app.post("/api/manual-paper-order")
async def manual_paper_order(order: ManualPaperOrder):
    try:
        return await engine.manual_order(order.action.lower(), order.side.lower(), order.amount)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/agents")
async def create_agent(agent: AgentCreate):
    try:
        kind,threshold,policy=agent.kind,agent.threshold,agent.policy
        if agent.template_id:
            template=next((item for item in engine.catalog()["templates"]
                           if item["id"]==agent.template_id),None)
            if not template:
                raise ValueError("Unknown bot template")
            kind=template["kind"]
            threshold=template["threshold"] if threshold is None else threshold
            policy={**template["policy"],**(policy or {})}
        if not kind:
            raise ValueError("Choose a bot template")
        return await engine.create_agent(agent.name,kind,agent.budget,
                                         threshold or .03,agent.deployed,policy)
    except ValueError as exc:
        raise HTTPException(status_code=400,detail=str(exc)) from exc


@app.get("/api/agents/{name}")
async def agent_detail(name: str):
    try:
        return await engine.agent_dossier(name)
    except ValueError as exc:
        raise HTTPException(status_code=404,detail=str(exc)) from exc


@app.post("/api/agents/{name}/control")
async def control_agent(name: str,control: AgentControl):
    try:
        return await engine.control_agent(name,control.action,control.budget,
                                          control.threshold,control.policy,
                                          control.delta,control.flatten,
                                          control.kind,control.template_id)
    except ValueError as exc:
        raise HTTPException(status_code=400,detail=str(exc)) from exc


@app.get("/api/fleet")
async def fleet_status():
    return fleet.status()


@app.post("/api/fleet/chat")
async def fleet_chat(body: FleetChat):
    return await fleet.chat(body.text, reset=body.reset)


@app.post("/api/fyften/transcribe")
async def fyften_transcribe(clip: VoiceClip):
    import base64
    try:
        audio = base64.b64decode(clip.audio_b64)
        return await transcribe_audio(audio, clip.content_type, list(engine.experiments))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Speech backend failed: {exc}") from exc


@app.post("/api/fyften/talk")
async def fyften_talk(clip: VoiceClip):
    import base64
    try:
        audio = base64.b64decode(clip.audio_b64)
        heard = await transcribe_audio(audio, clip.content_type, list(engine.experiments))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Speech backend failed: {exc}") from exc
    chat = await fleet.chat(heard["text"])
    return {**chat, "transcript": heard["text"], "stt_provider": heard.get("provider")}


@app.get("/api/chart")
async def chart():
    ticker = engine.current or ""
    return await store.rows("""SELECT ts,brti,target,p_yes,p_terminal,p_settlement,p_trend,
        confidence,yes_bid,yes_ask,settlement_average
        FROM features WHERE market_ticker=? ORDER BY ts DESC LIMIT 1200""", (ticker,))


@app.get("/api/markets")
async def markets():
    return await store.rows("SELECT * FROM markets ORDER BY close_ts DESC LIMIT 100")


@app.get("/api/markets/{ticker}")
async def market_detail(ticker: str):
    return {
        "market": await store.one("SELECT * FROM markets WHERE ticker=?", (ticker,)),
        "features": await store.rows("""SELECT * FROM features WHERE market_ticker=?
            ORDER BY ts LIMIT 5000""", (ticker,)),
        "fills": await store.rows("SELECT * FROM fills WHERE market_ticker=? ORDER BY fill_ts", (ticker,)),
        "positions": await store.rows("SELECT * FROM positions WHERE market_ticker=?", (ticker,)),
        "closed_trades": await store.rows(
            "SELECT * FROM closed_trades WHERE market_ticker=? ORDER BY close_ts", (ticker,)),
    }


@app.get("/api/analytics")
async def analytics():
    summary = []
    for exp in list(engine.experiments.values()):
        row = await store.one("""SELECT COUNT(*) trades,COALESCE(SUM(fee),0) fees,
            COALESCE(SUM(spread_cost),0) spread_cost,COALESCE(AVG(price),0) avg_price
            FROM fills WHERE experiment=?""", (exp.name,))
        settled = await store.one("""SELECT COUNT(*) settled,
            SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins,
            SUM(CASE WHEN pnl<=0 THEN 1 ELSE 0 END) losses,
            COALESCE(SUM(pnl),0) net_pnl,
            COALESCE(SUM(CASE WHEN pnl>0 THEN pnl ELSE 0 END),0) gross_win,
            COALESCE(-SUM(CASE WHEN pnl<0 THEN pnl ELSE 0 END),0) gross_loss
            FROM closed_trades WHERE experiment=?""", (exp.name,))
        avg_edge = await store.one("SELECT COALESCE(AVG(net_edge),0) value FROM decisions WHERE experiment=? AND action!='HOLD'", (exp.name,))
        curve = await store.rows("SELECT ts,equity FROM equity WHERE experiment=? ORDER BY ts", (exp.name,))
        peak,max_drawdown=exp.allocated_capital,0.0
        for point in curve:
            peak = max(peak, point["equity"])
            max_drawdown = max(max_drawdown, peak-point["equity"])
        ending_equity = curve[-1]["equity"] if curve else exp.cash
        summary.append({"strategy":exp.name,"starting_balance":exp.allocated_capital,
            "deployed":exp.deployed,"cash":exp.cash,
            "bank":exp.bank,"ending_equity":ending_equity,
            "total_equity":ending_equity+exp.bank,
            "realized":exp.realized,**(row or {}),**(settled or {}),
            "gross_pnl":(settled or {}).get("net_pnl",0)+(row or {}).get("fees",0),
            "avg_pnl_trade":((settled or {}).get("net_pnl",0)/max(1,(settled or {}).get("settled",0))),
            "max_drawdown":max_drawdown,"avg_estimated_edge":avg_edge["value"],"equity_curve":curve[::10]})
    calibration = await store.rows("""SELECT CAST(p_yes*10 AS INT)/10.0 bucket,COUNT(*) n,
        AVG(CASE WHEN m.result='yes' THEN 1.0 ELSE 0.0 END) actual,
        AVG((p_yes-(CASE WHEN m.result='yes' THEN 1.0 ELSE 0.0 END))*
            (p_yes-(CASE WHEN m.result='yes' THEN 1.0 ELSE 0.0 END))) brier
        FROM features f JOIN markets m ON m.ticker=f.market_ticker
        WHERE m.result IN ('yes','no') GROUP BY bucket ORDER BY bucket""")
    model_scores = await store.one("""WITH ranked AS (
        SELECT f.*,CASE WHEN m.result='yes' THEN 1.0 ELSE 0.0 END outcome,
          ROW_NUMBER() OVER (
            PARTITION BY f.market_ticker
            ORDER BY ABS(f.seconds_left-120),f.id) rank
        FROM features f JOIN markets m ON m.ticker=f.market_ticker
        WHERE m.result IN ('yes','no') AND f.data_fresh=1 AND f.target_valid=1),
      snapshots AS (SELECT * FROM ranked WHERE rank=1)
        SELECT COUNT(*) markets,AVG((p_yes-outcome)*(p_yes-outcome)) ensemble_brier,
          AVG((p_terminal-outcome)*(p_terminal-outcome)) terminal_brier,
          AVG((p_settlement-outcome)*(p_settlement-outcome)) settlement_brier,
          AVG(confidence) average_confidence FROM snapshots""")
    attribution = await store.rows("""SELECT c.experiment,c.side,c.pnl,d.net_edge,
        x.seconds_left,x.volatility,ABS(x.brti-x.target)/NULLIF(x.target,0) norm_distance,
        x.momentum_5s,x.spread,x.imbalance,x.regime
        FROM closed_trades c
        LEFT JOIN decisions d ON d.id=(SELECT id FROM decisions WHERE experiment=c.experiment
          AND market_ticker=c.market_ticker AND ts<=c.close_ts ORDER BY ts DESC LIMIT 1)
        LEFT JOIN features x ON x.id=(SELECT id FROM features WHERE market_ticker=c.market_ticker
          AND ts<=c.close_ts ORDER BY ts DESC LIMIT 1)""")
    def bucket(name, value):
        if name == "time_remaining": return f"{int(value//60)}m"
        if name == "estimated_edge": return f"{int(value*20)*5}%"
        if name in ("volatility","norm_distance"): return f"{value:.5f}"
        if name == "momentum": return "up" if value > 0 else "down"
        if name == "spread": return f"{int(value*20)*5}%"
        if name == "imbalance": return "buy" if value > .15 else ("sell" if value < -.15 else "neutral")
        return str(value)
    dimensions = {"strategy":"experiment","side":"side","time_remaining":"seconds_left",
        "estimated_edge":"net_edge","volatility":"volatility","norm_distance":"norm_distance",
        "momentum":"momentum_5s","spread":"spread","imbalance":"imbalance","regime":"regime"}
    grouped = []
    for dimension, key in dimensions.items():
        bins = {}
        for row in attribution:
            if row.get(key) is None: continue
            label = bucket(dimension,row[key])
            bins.setdefault(label,[0,0.0]); bins[label][0] += 1; bins[label][1] += row["pnl"] or 0
        grouped += [{"dimension":dimension,"bucket":k,"n":v[0],"net_pnl":v[1]} for k,v in bins.items()]
    return {"strategies":summary,"calibration":calibration,"breakdowns":grouped,
            "model_scores":model_scores or {},"calibrator":engine.calibrator.status(),
            "warning":"One overnight sample cannot establish a profitable edge."}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            await websocket.send_text(json.dumps(await engine.snapshot(), default=str))
            await asyncio.sleep(1)
    except (WebSocketDisconnect, RuntimeError):
        pass


def main():
    uvicorn.run("btc15.app:app", host=s.host, port=s.port, reload=False)


if __name__ == "__main__":
    main()
