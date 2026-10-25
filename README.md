# Hunter Signal Lab

Detects **liquidity events** (sweeps, grabs, breakouts, failed breakouts) on real crypto
candles and shows them on a live chart. This is **Phase 1: the foundation**. It records
what happened. It does not predict what happens next.

**Live demo:** `<YOUR-FRONTEND-URL>`  
**API:** `<YOUR-BACKEND-URL>/docs`  
Free hosting sleeps when idle, so the first load can take 30 to 60 seconds.

## What it does

- Fetches **closed** OHLCV candles from a public exchange API through CCXT (no API keys).
- Finds swing highs and lows, and clusters of equal highs and lows, as liquidity levels.
- Detects five event types with explicit rules, using only information that was
  available at the time (no look-ahead).
- Serves everything through a tested FastAPI backend and a React + TypeScript dashboard.

## What it does NOT do (yet)

- **No predictions.** The "TabPFN prediction engine" panel says *Not connected* on purpose.
  Predictions will only be added after we measure what price did after each event and
  test whether any model beats a simple baseline.
- No database (history is whatever the current candle window contains).
- No login or accounts. The watchlist lives in your browser only (not synced across devices).
- No background monitoring or alerts. No trading.
- Only the configured symbols: six USDT pairs by default (BTC, ETH, SOL, BNB, XRP, DOGE).

## Dashboard

- Candlestick chart with liquidity levels and event markers (all times UTC).
- **Top 5 events (last 24 hours):** ranked by volume ratio only. It is an event-priority
  list, **not a prediction or a trade signal**. Click an item to open it.
- **Watchlist:** star a coin to keep it handy; filter the Top 5 to your watchlist.
  Saved in your browser's local storage, no login.
- Event explorer with search and type filter, and a JSON export of the selected view.

## API

| Endpoint | Returns |
|---|---|
| `GET /health` | status and exchange |
| `GET /api/markets` | supported symbols and timeframes |
| `GET /api/candles?symbol=&timeframe=&limit=` | closed candles |
| `GET /api/analysis?symbol=&timeframe=&limit=` | levels and events for one market |
| `GET /api/events` | events across markets, newest first (filters: symbol, timeframe, event_type, limit) |
| `GET /api/events/{event_id}` | one event |
| `GET /api/summary` | counts by type |
| `GET /brief?symbols=BTC/USDT,ETH/USDT` | two-sentence evidence brief for the top three watchlist events; returns a clear error if Gemma is unavailable (template fallback is opt-in) |

Timeframes: `5m`, `15m`, `1h`, `4h`. Errors return `{"detail": "...", "code": "..."}`.

## Gemma brief configuration

The backend uses Google's Gen AI SDK to call the Gemma model configured by `BRIEF_MODEL` in `backend/.env`. Keep `GEMMA_API_KEY` in the backend environment only; never place model names or provider API keys in the frontend environment. The frontend only needs `VITE_API_BASE_URL`.

Template fallback is disabled by default. If Gemma fails or returns invalid output, the API returns an HTTP error containing the reason and the UI displays it in red. To explicitly permit deterministic template fallback, set `ALLOW_TEMPLATE_FALLBACK=true` in the backend environment; otherwise leave it unset or set it to `false`.

## Event rules

Defaults: swing bars **N = 3**, equal-level tolerance **0.10%**, confirmation window
**K = 3** candles, breakout buffer **0.10%**, volume lookback **20**.
These are project choices, **not industry standards, and they have not been backtested**.

- Only closed candles are analysed, and they must be exactly one timeframe apart.
- A swing high has a strictly higher high than the N candles on each side. It becomes
  usable only after the N-th candle to its right has closed.
- Same-side swings within the tolerance merge into an equal-highs / equal-lows cluster,
  but only if nobody has pierced the level yet.
- A level resolves once, on the first candle that pierces it (high side shown; low side mirrors):
  1. Close above the level plus the buffer: **BREAKOUT**. If a close at or below the
     level follows within K candles: **FAILED_BREAKOUT**.
  2. Close at or below the level, single swing: **SWING_HIGH_SWEEP**.
  3. Close at or below the level, cluster: **LIQUIDITY_GRAB**.
  4. Close between the level and the buffer, single swing: no event.
  5. Close between the level and the buffer, cluster: wait up to K candles for a grab or breakout.
- Every event has `timestamp` (open of the triggering candle) and `detected_at` (its
  close, when it became knowable). Use `detected_at` for any backtest.
- Tests compare `analyse(candles[:n])` with the full run for every n, to prove no
  event depends on future candles.

## Run locally (Windows PowerShell)

```powershell
# backend
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --port 8000

# frontend (second window)
cd frontend
npm install
npm run dev        # http://localhost:5173
```

Tests: `cd backend; python -m pytest -q` (no network needed; the exchange is mocked).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `HUNTER_EXCHANGE` | `binance` | CCXT exchange id |
| `HUNTER_SYMBOLS` | `BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT,DOGE/USDT` | symbol allowlist (each coin adds exchange requests) |
| `HUNTER_CACHE_TTL` | `15` | seconds to cache exchange responses |
| `HUNTER_EVENT_TIMEFRAMES` | `15m,1h` | timeframes used by `/api/events` |
| `HUNTER_CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | allowed browser origins |
| `VITE_API_BASE_URL` | `http://localhost:8000` | backend URL baked into the frontend at build time |

## Deploy on Render

`render.yaml` defines a Python web service (backend) and a static site (frontend).

1. Push the repo to GitHub. In Render choose **New, Blueprint** and select the repo.
2. Keep the backend in **Frankfurt or Singapore**. Some exchanges block US servers.
3. After the backend is live, set the static site's `VITE_API_BASE_URL` to the backend URL
   and redeploy (the value is baked in at build time).
4. Set the backend's `HUNTER_CORS_ORIGINS` to the frontend URL.
5. Check `<backend>/api/candles?symbol=BTC/USDT&timeframe=15m&limit=5`. If the exchange
   is blocked from your region, set `HUNTER_EXCHANGE` to another CCXT id and redeploy.

## Gemma brief and chart snapshots

Click **Brief** to request an evidence-only explanation of the top three detected events. The API reports Gemma service status at `/api/ai/health`; `/health` includes the same Gemma health result. If Gemma is unavailable or returns invalid output, the API returns a clear error by default rather than silently using a template. Template fallback is available only when `ALLOW_TEMPLATE_FALLBACK=true` is explicitly set in the backend environment. Each selected event can include a server-rendered SVG chart snapshot with the event price and linked level marked. Snapshots are generated locally by the backend and embedded in the brief response, so no chart-image service is used. Export report includes the brief and snapshot data URIs when a brief has been generated.


## Known limitations

- No database: an event ID stops resolving once its candle leaves the current window (about 500 candles).
- A breakout can gain a FAILED_BREAKOUT up to K candles later. Events never change once emitted.
- Thresholds are fixed percentages. They are not scaled by volatility.
- Results depend on the candle window: the same candle can be classified differently if an older swing falls outside the window, so counts can differ slightly between views.
- Event counts are observations, not signals. Nothing here is financial advice.

## Roadmap (not built yet)

- Watchlist synced across devices (needs accounts)
- Market search and more coins than the default six
- Persistent event history
- Outcome measurement (what price did after each event, using `detected_at` only)
- Volatility-scaled thresholds (ATR)
- Prediction models (TabPFN) **only after** outcome evaluation exists
- Login, only when there is per-user data to protect
- Background monitoring, ranking and reports
