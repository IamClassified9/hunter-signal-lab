# Contributing to Hunter Signal Lab

Thanks for helping. This project values **honest, tested** work over quick features.

## Setup

See the README ("Run locally"). Before opening a pull request:

```powershell
cd backend
python -m pytest -q        # must pass
cd ../frontend
npm run build              # must pass
```

## Ground rules

- **Only closed candles.** Never analyse a forming candle.
- **No look-ahead.** An event may only use information available at its `detected_at`.
  Any engine change must keep the prefix-comparison tests green.
- **Convert exchange timestamps in one place only**
  (`_ccxt_timestamp_to_utc` in the backend, one helper in `frontend/src/services/api.ts`).
- **Engine rules and `schemas.py` change only by agreement.** Open an issue first and
  explain the change and why the tests still prove it is safe.
- **No fake data and no unvalidated claims.** Do not label anything as a prediction.
- Tests must not need network access. Mock the exchange.
- Keep pull requests small and focused, with a description of what changed and why.

## Good first issues

1. **Watchlist sync.** The watchlist is browser-only today. Design how it could be saved per user.
2. **Market search.** Let users search and add pairs beyond the default six, with sensible rate limits.
3. **Malformed-candle tests.** Add cases for odd exchange responses
   (missing fields, strings instead of numbers, duplicated rows).

Also welcome: documentation fixes, and an extra timeframe with tests.

## Reporting bugs

Open an issue with the symbol, timeframe, the time you saw it (UTC), what you expected,
and what happened. Screenshots of the chart help.
