from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import auth
from .config import Settings
from .engine import Engine
from .fleet import Fleet
from .storage import Store

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
    name: str = Field(min_length=2, max_length=32)
    template_id: str | None = None
    kind: str | None = None
    budget: float = Field(ge=1, le=10_000)
    threshold: float | None = Field(default=None, ge=.005, le=.20)
    deployed: bool = True
    policy: dict[str, float] | None = None


class AgentControl(BaseModel):
    action: str
    budget: float | None = Field(default=None, ge=1, le=10_000)
    threshold: float | None = Field(default=None, ge=.005, le=.20)
    policy: dict[str, float] | None = None
    delta: float | None = Field(default=None, ge=.01, le=10_000)
    flatten: bool = False
    kind: str | None = None
    template_id: str | None = None


class FleetChat(BaseModel):
    text: str = Field(min_length=0, max_length=4000)
    reset: bool = False


class LoginBody(BaseModel):
    username: str = ""
    password: str = ""


@asynccontextmanager
async def lifespan(_: FastAPI):
    await store.open()
    await engine.start()
    yield
    await engine.stop()
    await store.close()


app = FastAPI(title="fyfteen labs", lifespan=lifespan)
auth.install(app)


def _guard(request: Request, mutating: bool = False) -> None:
    auth.require_user(request)
    if mutating:
        auth.require_csrf(request)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    from importlib.resources import files
    if auth.logged_in(request):
        return RedirectResponse("/", status_code=303)
    return (files("btc15") / "login.html").read_text(encoding="utf-8")


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    username = str(form.get("username") or "")
    password = str(form.get("password") or "")
    if not auth.verify_login(username, password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    auth.login(request, username or auth.settings().fyfteen_user)
    return RedirectResponse("/", status_code=303)


@app.post("/api/login")
async def api_login(request: Request, body: LoginBody):
    if not auth.verify_login(body.username, body.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    auth.login(request, body.username or auth.settings().fyfteen_user)
    return {"ok": True, "user": request.session.get("user"), "csrf": auth.csrf_token(request)}


@app.api_route("/logout", methods=["GET", "POST"])
async def logout(request: Request):
    auth.logout(request)
    return RedirectResponse("/login" if auth.enabled() else "/", status_code=303)


@app.get("/api/session")
async def session(request: Request):
    return {
        "auth_required": auth.enabled(),
        "ok": auth.logged_in(request),
        "user": request.session.get("user") if auth.logged_in(request) else None,
        "csrf": auth.csrf_token(request) if auth.logged_in(request) else "",
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if auth.enabled() and not auth.logged_in(request):
        return RedirectResponse("/login", status_code=303)
    from importlib.resources import files
    return (files("btc15") / "dashboard.html").read_text(encoding="utf-8")


@app.get("/api/state")
async def state(request: Request):
    _guard(request)
    return await engine.snapshot()


@app.get("/api/catalog")
async def catalog(request: Request):
    _guard(request)
    return engine.catalog()


@app.post("/api/manual-paper-order")
async def manual_paper_order(request: Request, order: ManualPaperOrder):
    _guard(request, True)
    try:
        return await engine.manual_order(order.action.lower(), order.side.lower(), order.amount)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/agents")
async def create_agent(request: Request, agent: AgentCreate):
    _guard(request, True)
    try:
        kind, threshold, policy = agent.kind, agent.threshold, agent.policy
        if agent.template_id:
            template = next((item for item in engine.catalog()["templates"]
                             if item["id"] == agent.template_id), None)
            if not template:
                raise ValueError("Unknown bot template")
            kind = template["kind"]
            threshold = template["threshold"] if threshold is None else threshold
            policy = {**template["policy"], **(policy or {})}
        if not kind:
            raise ValueError("Choose a bot template")
        return await engine.create_agent(agent.name, kind, agent.budget,
                                         threshold or .03, agent.deployed, policy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/agents/{name}")
async def agent_detail(request: Request, name: str):
    _guard(request)
    try:
        return await engine.agent_dossier(name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/agents/{name}/control")
async def control_agent(request: Request, name: str, control: AgentControl):
    _guard(request, True)
    try:
        return await engine.control_agent(name, control.action, control.budget,
                                          control.threshold, control.policy,
                                          control.delta, control.flatten,
                                          control.kind, control.template_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/fleet")
async def fleet_status(request: Request):
    _guard(request)
    return fleet.status()


@app.post("/api/fleet/chat")
async def fleet_chat(request: Request, body: FleetChat):
    _guard(request, True)
    return await fleet.chat(body.text, reset=body.reset)


@app.get("/api/chart")
async def chart(request: Request):
    _guard(request)
    ticker = engine.current or ""
    return await store.rows("""SELECT ts,brti,target,p_yes,p_terminal,p_settlement,p_trend,
        confidence,yes_bid,yes_ask,settlement_average
        FROM features WHERE market_ticker=? ORDER BY ts DESC LIMIT 1200""", (ticker,))


@app.get("/api/markets")
async def markets(request: Request):
    _guard(request)
    return await store.rows("SELECT * FROM markets ORDER BY close_ts DESC LIMIT 100")


@app.get("/api/markets/{ticker}")
async def market_detail(request: Request, ticker: str):
    _guard(request)
    return {
        "market": await store.one("SELECT * FROM markets WHERE ticker=?", (ticker,)),
        "features": await store.rows("""SELECT * FROM features WHERE market_ticker=?
            ORDER BY ts LIMIT 5000""", (ticker,)),
        "fills": await store.rows("SELECT * FROM fills WHERE market_ticker=? ORDER BY fill_ts", (ticker,)),
        "closed_trades": await store.rows(
            "SELECT * FROM closed_trades WHERE market_ticker=? ORDER BY close_ts", (ticker,)),
    }


@app.get("/api/analytics")
async def analytics(request: Request):
    _guard(request)
    summary = []
    for exp in list(engine.experiments.values()):
        row = await store.one("""SELECT COUNT(*) trades,COALESCE(SUM(fee),0) fees
            FROM fills WHERE experiment=?""", (exp.name,))
        settled = await store.one("""SELECT COUNT(*) settled,
            SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins,
            SUM(CASE WHEN pnl<=0 THEN 1 ELSE 0 END) losses,
            COALESCE(SUM(pnl),0) net_pnl
            FROM closed_trades WHERE experiment=?""", (exp.name,))
        curve = await store.rows(
            "SELECT ts,equity FROM equity WHERE experiment=? ORDER BY ts", (exp.name,))
        peak, max_drawdown = exp.allocated_capital, 0.0
        for point in curve:
            peak = max(peak, point["equity"])
            max_drawdown = max(max_drawdown, peak - point["equity"])
        ending = curve[-1]["equity"] if curve else exp.cash
        settled_n = (settled or {}).get("settled") or 0
        summary.append({
            "strategy": exp.name, "template": exp.kind, "cash": exp.cash,
            "equity": ending + exp.bank, "trades": (row or {}).get("trades") or 0,
            "fees": (row or {}).get("fees") or 0, **(settled or {}),
            "win_rate": ((settled or {}).get("wins") or 0) / max(1, settled_n),
            "avg_pnl_trade": ((settled or {}).get("net_pnl") or 0) / max(1, settled_n),
            "max_drawdown": max_drawdown,
        })
    calibration = await store.rows("""SELECT CAST(p_yes*10 AS INT)/10.0 bucket,COUNT(*) n,
        AVG(CASE WHEN m.result='yes' THEN 1.0 ELSE 0.0 END) actual
        FROM features f JOIN markets m ON m.ticker=f.market_ticker
        WHERE m.result IN ('yes','no') GROUP BY bucket ORDER BY bucket""")
    return {"strategies": summary, "calibration": calibration}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    if auth.enabled() and not (getattr(websocket, "session", None) or {}).get("user"):
        await websocket.close(code=4401)
        return
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
