# Hunter Signal Lab

Hunter Signal Lab detects **liquidity events**—sweeps, grabs, breakouts, and failed breakouts—from real crypto candles and displays the evidence on an interactive chart.

**Current scope: Phase 1 — event detection and evidence review.** The system records and explains observed market events; it does not predict future prices or generate trade signals.

## Live deployment

- **Live dashboard:** https://hunter-signal-lab-web.onrender.com
- **Backend API:** https://hunter-signal-lab-api.onrender.com/
- **Interactive API documentation:** https://hunter-signal-lab-api.onrender.com/docs
- **Source code:** https://github.com/IamClassified9/hunter-signal-lab

The services use free hosting, which may sleep while idle. The first request after inactivity can take 30–60 seconds.

## What it does

- Fetches closed OHLCV candles from a public exchange through CCXT; no exchange API keys are required for public market data.
- Identifies swing highs and lows, plus equal-high/equal-low liquidity levels.
- Detects five event types using explicit rules and information available at the time, avoiding look-ahead in event detection.
- Exposes market data and analysis through a FastAPI backend and a React + TypeScript dashboard.
- Displays candles, liquidity levels, and event markers on the chart, with timestamps in UTC.
- Provides a watchlist stored in the current browser, a Top 5 event-priority view, event search and filtering, JSON export, and an evidence-focused brief.

## What it does not do yet

- **No predictive model is connected.** The dashboard's “Prediction engine (later phase)” panel is intentionally marked **Not Connected**.
- **TabPFN was removed from the current implementation.** There was not enough time in this delivery window to implement and validate the full outcome-labeling and prediction workflow. A meaningful prediction model needs a defined target (what counts as an outcome), historical events paired with what price did afterward, leakage-safe features, and evaluation against a simple baseline. Adding a model before those pieces are implemented and tested would risk presenting unvalidated results as predictions. TabPFN remains a possible future option, not a working feature in this release.
- No persistent database: available event history depends on the current candle window.
- No user accounts or login. The watchlist is stored in browser local storage and is not synced across devices.
- No background monitoring, alerts, or automated trading.
- The default configured market list contains six USDT pairs: BTC, ETH, SOL, BNB, XRP, and DOGE.

## Dashboard

- **Signal overview:** summary metrics and event activity for the selected market and timeframe.
- **Liquidity chart:** candlesticks with detected liquidity levels and event markers.
- **Top 5 events (last 24 hours):** ranked by volume ratio only. This is an event-priority list, **not a prediction or trade signal**. Select an event to inspect it.
- **Watchlist:** star pairs to keep them handy and optionally filter the Top 5 list to watched pairs. The watchlist is stored in this browser only; no login is required.
- **Event explorer:** search and filter recorded events, inspect supplied event attributes, and export the selected view as JSON.
- **Liquidity brief:** an evidence-focused explanation of selected detected events. It is not a price forecast. If the configured Gemma service is unavailable, behavior depends on the backend's template-fallback setting.

## API

Interactive documentation: https://hunter-signal-lab-api.onrender.com/docs

| Endpoint | Returns |
|---|---|
| `GET /health` | Service status and exchange information |
| `GET /api/markets` | Supported symbols and timeframes |
| `GET /api/candles?symbol=&timeframe=&limit=` | Closed candles |
| `GET /api/analysis?symbol=&timeframe=&limit=` | Liquidity levels and events for one market |
| `GET /api/events` | Events across markets, newest first; supports symbol, timeframe, event type, and limit filters |
| `GET /api/events/{event_id}` | A single event |
| `GET /api/summary` | Event and market summary counts |
| `GET /brief?symbols=BTC/USDT,ETH/USDT` | Evidence brief for up to the top three relevant events, subject to configured brief service and fallback behavior |

Supported timeframes: `5m`, `15m`, `1h`, and `4h`. API errors use a JSON response with `detail` and `code` fields where provided by the application.

## Event rules

Default parameters:

- Swing bars: **N = 3**
- Equal-level tolerance: **0.10%**
- Confirmation window: **K = 3 candles**
- Breakout buffer: **0.10%**
- Volume lookback: **20 candles**

These are project-specific choices, not industry standards, and have not been established as profitable through backtesting.

- Only closed candles are analysed, and candles must be exactly one timeframe apart.
- A swing high must exceed the highs of the N candles on either side. It becomes usable only after the Nth candle to its right closes.
- Same-side swings within the tolerance can merge into equal-high/equal-low clusters, provided the level has not already been pierced.
- A level resolves on its first qualifying pierce. High-side rules are mirrored for low-side levels:
  1. A close above the level plus the buffer is a **breakout**. If a close at or below the level follows within K candles, it can be classified as a **failed breakout**.
  2. A close at or below the level for a single swing produces a **swing sweep**.
  3. A close at or below the level for a cluster produces a **liquidity grab**.
  4. A close between the level and buffer for a single swing produces no event.
  5. For a cluster closing between the level and buffer, the engine waits up to K candles for a grab or breakout.
- Each event has `timestamp` (the triggering candle's open) and `detected_at` (when the event became knowable at candle close). Use `detected_at` for any future outcome evaluation or backtesting.
- The test suite compares `analyse(candles[:n])` with the full run for each tested prefix to check that event detection does not depend on future candles.

## Gemma brief configuration

The backend uses Google's Gen AI SDK to call the configured Gemma model. `BRIEF_MODEL` selects the model, and `GEMMA_API_KEY` must remain in the backend environment—never put provider API keys in frontend code or frontend environment variables. The frontend uses `VITE_API_BASE_URL` to locate the backend.

Template fallback is disabled by default. If Gemma fails or returns invalid output, the API returns an HTTP error unless deterministic template fallback is explicitly enabled with `ALLOW_TEMPLATE_FALLBACK=true` in the backend environment. Leave it unset or set it to `false` to keep fallback disabled.

The brief is intended to explain the supplied event evidence, not to forecast price. The backend may include a server-generated chart snapshot with an event and linked liquidity level marked. The snapshot is generated locally by the backend rather than through an external chart-image service.

## Run locally (Windows PowerShell)

Open two terminals from the repository root.

**Backend terminal:**

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --port 8000
```

**Frontend terminal:**

```powershell
cd frontend
npm install
npm run dev
```

Vite normally serves the frontend at `http://localhost:5173`.

Run backend tests from the repository root:

```powershell
cd backend
python -m pytest -q
```

The test suite mocks exchange access so tests do not require live exchange requests.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `HUNTER_EXCHANGE` | `binance` | CCXT exchange ID |
| `HUNTER_SYMBOLS` | `BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT,DOGE/USDT` | Configured symbol allowlist; each additional pair increases exchange requests |
| `HUNTER_CACHE_TTL` | `15` | Exchange-response cache lifetime in seconds |
| `HUNTER_EVENT_TIMEFRAMES` | `15m,1h` | Timeframes used by `/api/events` |
| `HUNTER_CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | Allowed browser origins; include the deployed frontend origin in production |
| `VITE_API_BASE_URL` | `http://localhost:8000` | Backend URL baked into the frontend at build time |

## Deploy on Render

The `render.yaml` blueprint defines a Python backend service and a static frontend site.

1. Push the repository to GitHub and create a Render Blueprint from the repository.
2. Choose a backend region supported by the exchange access available to the service.
3. Set the frontend service's `VITE_API_BASE_URL` to `https://hunter-signal-lab-api.onrender.com` and redeploy the frontend. This value is baked in at build time.
4. Set the backend's `HUNTER_CORS_ORIGINS` to `https://hunter-signal-lab-web.onrender.com`.
5. Verify the backend health endpoint and API documentation:
   - https://hunter-signal-lab-api.onrender.com/health
   - https://hunter-signal-lab-api.onrender.com/docs

If the exchange is blocked or unavailable from the service region, configure `HUNTER_EXCHANGE` to another supported CCXT exchange and redeploy.

## Roadmap

The roadmap prioritizes validating the existing event engine and building reliable outcomes before adding prediction or account features.

1. **Validate the deployed foundation**
   - Verify the frontend-to-backend connection, CORS configuration, and exchange data access.
   - Keep the event chart, linked liquidity levels, event details, and UTC timestamps consistent with the API response.
2. **Build historical outcome measurement**
   - Persist or reproducibly collect detected events and the candles following each event.
   - Define outcome horizons and labels before training a model.
   - Use `detected_at` to prevent using information that was not available when an event was detected.
3. **Evaluate prediction feasibility**
   - Establish a simple baseline and leakage-safe evaluation split.
   - Consider TabPFN or another suitable model only after outcome labels and evaluation are implemented.
   - Report measured results and limitations; do not expose predictions as reliable until validated.
4. **Add persistent event history**
   - Store events and supporting market data so records survive beyond the active candle window.
5. **Add monitoring and alerts**
   - Consider scheduled background analysis, notifications, and configurable reporting after data reliability is established.
6. **Consider user accounts only if needed**
   - Introduce authentication when the product needs server-side per-user watchlists, saved settings, or other private synced data. Login is not a goal by itself.

## Known limitations

- There is no database, so event history is limited to the active candle window; an event may stop resolving after its source candles leave that window.
- A breakout can later be followed by a failed-breakout event within K candles. The engine does not revise already emitted events.
- Thresholds are fixed percentages and are not volatility-scaled.
- Results depend on the candle window. A candle can be classified differently if an older swing falls outside the window, so event counts may vary between views.
- Event rankings and event counts describe observations; they are not trade signals. Nothing here is financial advice.
