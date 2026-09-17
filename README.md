# Fyfteen Labs

A local sandbox for **paper-testing automated strategies** on [Kalshi](https://kalshi.com)'s recurring 15-minute Bitcoin markets.

It uses **real production market data** and **simulated funds only**. Bots never place live orders. This is a resume / systems-engineering project, not a trading product, and it does not claim profitability.

Live desk: [http://127.0.0.1:8000](http://127.0.0.1:8000)

---

## Why this project exists

Prediction-market bots look simple until you have to keep a real-time book, a 15-minute contract clock, and a settlement definition in sync.

Fyfteen Labs is a place to practice that kind of production-shaped work:

- ingest a live external market over REST + WebSocket
- keep local state honest across reconnects, rollovers, and process restarts
- run deterministic strategies against a realistic paper broker
- let an LLM operate the system **only** through schema-validated tools
- explain every decision in plain English so the demo is inspectable

It is useful as a portfolio piece for backend, data, and applied-LLM engineering. It is not a signal that these templates would make money.

---

## What it demonstrates

| Area | What you can point to in the code |
| --- | --- |
| **Async market data** | Kalshi production REST + WebSocket, RSA-PSS signed reads, automatic reconnect |
| **Market state** | BTC15 discovery, 15-minute rollover, YES/NO book, strike, countdown |
| **Reference data** | Official BRTI feed and the 60-second end-average used for settlement |
| **Event-driven engine** | Order-book snapshots/deltas, sequence checks, isolated strategy evaluation |
| **Deterministic bots** | Three templates with a short path: data health → signal → edge → risk → paper trade |
| **Realistic simulation** | Fill at the visible ask (never midpoint), walk depth, latency, quadratic taker fees |
| **Accounting** | Positions, settlement from the official outcome, P&L, persistence in SQLite |
| **Replay** | Recorded markets can be replayed into a separate database without mutating the source |
| **LLM tool use** | FYFTEN configures the Fleet through structured tools; it cannot write Python or invent odds |
| **Auth / boundaries** | Optional local session + CSRF; Kalshi and LLM secrets stay on the server |
| **Dashboard** | Market, Fleet, History, manual paper desk, decision reasons, “why didn’t it trade?” |
| **Tests** | Discovery, books, fees, fills, settlement, templates, tools, auth |

The Kalshi client is **GET / WebSocket subscribe only**. There is no order endpoint and no live-trading switch.

---

## Real data / fake money

| Real (read-only) | Simulated (local) |
| --- | --- |
| Live BTC15 contract discovery | Per-bot paper cash |
| YES/NO order book | Fills against the visible book |
| BRTI + official end-average | Fees, positions, settlement, P&L |
| Contract schedule and strike | Deploy / pause / retire |

That split is the point of the project: keep the hard integration real, keep the money fake, and make the boundary obvious.

---

## Architecture

One local FastAPI app. Not microservices — just clear responsibilities:

```
Market data     kalshi.py, discover / stream / rollover
      ↓
Evaluation      catalog, model, engine.decide
      ↓
Paper broker    book walk, latency, fees, settlement
      ↓
Fleet           create / pause / edit / retire
      ↓
FYFTEN tools    schema-validated chatbot actions
      ↓
SQLite          markets, features, decisions, fills, equity
      ↓
Dashboard       app.py + dashboard.html
```

Bots stay deterministic. The LLM is a controller, not a trader.

---

## Fleet templates

The UI is template-first. Fair Value is the default.

| Template | Engineering idea | When it looks |
| --- | --- | --- |
| **Fair Value** | Distance to strike + time left + recent vol → P(YES) vs the live ask | Most of the 15-minute window |
| **Momentum** | Same leftover-edge rule, with a short Bitcoin-direction tilt | When BTC has been moving one way |
| **Late Settlement** | Uses Kalshi’s official 60-second BRTI average | Last 3 minutes only; quiet on purpose |

```
edge = model probability − executable ask − fee − safety margin
```

Every decision has a reason a person can read:

- `BUY YES. Model probability 72%. YES ask 64¢. Estimated edge after fees 5.8% (need 4.0%).`
- `HOLD. Estimated edge 1.3% is below the required 4.0%.`
- `WAIT. Market data is stale.`

Fleet detail also rolls up **Why didn’t it trade?** for the last 15 minutes (below edge, stale book, spread, cooldown, exposure, …).

---

## FYFTEN (text chatbot)

FYFTEN manages the Fleet in plain English. It never predicts Bitcoin, never submits orders, and never touches the database. It only calls tools such as `create_bot`, `pause_bot`, `add_cash`, `why_not_traded`, and `summarize_market`.

> Create a momentum bot called spike with $100.

becomes a structured call:

```text
create_bot(name="spike", template_id="momentum", budget=100)
```

and the UI shows the **actual** tool result. If the LLM is unavailable, a local compiler still handles the common commands.

---

## Paper execution

Paper buys:

1. wait out simulated latency
2. walk the visible ask (YES) or bid (NO)
3. charge Kalshi’s published quadratic taker fee
4. record position, mark-to-market, and settlement from the official outcome

You can also trade the current contract yourself from the Market tab. Same broker, still fake money.

Settlement reference: [Kalshi crypto markets](https://help.kalshi.com/en/articles/13823838-crypto-markets). BRTI methodology: [CME CF Real Time Indices](https://docs.cfbenchmarks.com/CME%20CF%20Real%20Time%20Indices%20Methodology.pdf).

---

## Replay

Recordings are immutable. Replay writes a separate database so you can re-run the engine deterministically.

```powershell
kalshi-replay data/paper.db --output data/replay.db --speed 100000 --markets 6
```

---

## Auth

Personal/local demo auth — enough to keep secrets off the page, not an identity platform.

| Env | Role |
| --- | --- |
| `FYFTEEN_USER` | Username (default `fyfteen`) |
| `FYFTEEN_PASSWORD` | If set, dashboard, WebSocket, and APIs require a session |
| `FYFTEEN_SESSION_SECRET` | Cookie signing; derived from the password if omitted |

When enabled: `/login`, `SameSite=lax` session cookie, `X-CSRF-Token` on mutations. Kalshi and LLM keys never go to the browser.

---

## Run locally

Python 3.11+ and a **read-only** Kalshi production API key.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
```

Create `.env` (gitignored — never commit it):

```dotenv
KALSHI_KEY_ID=your-key-id
KALSHI_PRIVATE_KEY_PATH=C:\secure\path\kalshi-private-key.pem
KALSHI_DB_PATH=data/paper.db

# optional
FYFTEEN_PASSWORD=
FIREWORKS_API_KEY=
```

```powershell
python -m btc15.app
pytest
```

SQLite lives at `data/paper.db`. Stop the desk with Ctrl+C.

---

## Safety

- No real order-submission code exists in this repo.
- Secrets belong in `.env`, `pvtkey/`, or a PEM path outside git.
- Paper P&L is bookkeeping for the simulator, not evidence of an edge.

---

## Demo loop

1. Open the live BTC15 market (BRTI, target, YES/NO, countdown).
2. On **Fleet**, pick Fair Value, give it fake cash, deploy.
3. Watch the last reason and **Why didn’t it trade?**
4. Ask FYFTEN: `Why didn’t my Momentum bot trade?`
