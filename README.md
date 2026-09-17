# fyfteen labs

Paper sandbox for [Kalshi](https://kalshi.com)'s recurring **15-minute Bitcoin** Above/Below market.

Real production market data. Fake money. No live order submission.

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) after you start the desk.

## What an opportunity is

An opportunity is **leftover edge after costs**, not a raw YES/NO guess.

```
leftover = forecast − executable ask − fee − safety margin − uncertainty
```

If leftover is not positive, or the book is stale, the correct action is wait.

| Job | What it watches | Window |
| --- | --- | --- |
| Direction | Persistent 5 / 30 / 60-second Bitcoin move | Last 6 minutes |
| Both agree | Direction and the official end-average on the same side | Last 4 minutes |
| Late closer | Official 60-reading BRTI average vs the strike | Last 3 minutes |

Each template watches **one bucket**. Order-book flow is used to time a fill, not to predict the whole 15 minutes.

This sandbox tests whether a thesis has evidence. It does not promise passive income.

## Run locally

Requires Python 3.11+ and a Kalshi production API key (read-only).

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
```

Create `.env`:

```dotenv
KALSHI_KEY_ID=your-key-id
KALSHI_PRIVATE_KEY_PATH=C:\secure\path\kalshi-private-key.pem
```

```powershell
python -m btc15.app
```

Leave that process running. Stop with Ctrl+C. SQLite lives at `data/paper.db`.

## Fleet and FYFTEN

The **Fleet** tab is the roster. Tap the **15** bubble to talk to FYFTEN — voice in, text out. It assigns jobs, funds agents, and retires them. Bots stay deterministic.

```dotenv
FIREWORKS_API_KEY=
GROQ_API_KEY=
FYFTEN_LLM_BASE_URL=https://api.fireworks.ai/inference/v1
FYFTEN_LLM_MODEL=accounts/fireworks/models/glm-5p3-flash
FYFTEN_STT_PROVIDER=groq
FYFTEN_STT_MODEL=whisper-large-v3-turbo
FYFTEN_STT_URL=https://api.groq.com/openai/v1/audio/transcriptions
```

FYFTEN talks to Fireworks GLM 5.3 Flash and transcribes with Groq `whisper-large-v3-turbo`. Without keys it still configures the fleet: the local compiler runs, and the browser speech API is used until you paste `GROQ_API_KEY`.

## Replay

```powershell
kalshi-replay data/paper.db --output data/replay.db --speed 100000 --markets 6
```

The input recording is never modified.

## Safety

- The Kalshi adapter is authenticated GET / WebSocket subscribe only. There is no order endpoint and no live-trading switch.
- Fees use Kalshi's published quadratic taker model.
- Settlement uses the official 60 one-second BRTI average in the final minute: [Kalshi crypto markets](https://help.kalshi.com/en/articles/13823838-crypto-markets).
- BRTI methodology: [CME CF Real Time Indices](https://docs.cfbenchmarks.com/CME%20CF%20Real%20Time%20Indices%20Methodology.pdf).

```powershell
pytest
```
