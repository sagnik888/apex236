# NSE F&O Multi-Timeframe Screener

Windows-ready FastAPI screener for the user's authoritative **237-stock NSE F&O list**.

## Current dashboard

Two independent boards: **Bullish / Buying Signals** and **Bearish / Selling Signals**.

Column order:

`Symbol | Price | Score | Signal | 5m | 15m | 30m | 1h | 4h | 1D% | 2D% | 4D% | 7D% | 30D% | RSI | ADX | RVOL | Freshness | Bar time | Why`

Every directional field refreshes from the latest completed scan. Intraday timeframe cells are independent, so a stock can be bullish on 5m and bearish on 1h without the UI hiding that disagreement.

## Refresh architecture

- live quote pulse: 15 seconds by default
- micro scan: 5m / 15m / 30m every 55 seconds
- medium scan: 1h / 4h every 120 seconds
- macro scan: 1d / 2d / 4d / 1w every 300 seconds
- lanes are independent; a macro scan cannot block the micro lane
- closed-bar sealing prevents incomplete candles from being scored as final

## 237-symbol universe contract

`data/instruments.json` is authoritative and contains 237 unique symbols. `data/nifty_fno_237.txt` is the supplied reference list.

The Upstox daily BOD master **enriches** those 237 rows with current instrument keys / nearest futures metadata. It never replaces or shrinks the list. A missing Upstox match remains in the universe and is reported in metadata rather than silently disappearing.

`universe_count` therefore means configured stocks. `scored_count` means stocks for which enough valid market history was available for that scan. `failed_symbols` exposes exactly which names could not be scored and why.

## Scoring engine v3

The score is a signed -100..+100 closed-bar composite using:

- EMA stack trend alignment
- EMA21/EMA50 slope
- short + medium ATR-normalized momentum
- MACD histogram level + acceleration
- RSI momentum confirmation
- position within the recent range
- +DI / -DI directional movement
- breakout/breakdown against the **prior** 20-bar range
- higher-timeframe trend confirmation when a non-stale slower state is available
- ADX as confidence only (never direction)
- RVOL against the **previous** 20 bars, used as bounded confirmation
- factor-agreement damping when meaningful components conflict

See `AUDIT_AND_CHANGES.md` for the bugs fixed in the earlier engine. See `app/engine/scoring_validation.py` for a no-lookahead walk-forward directional validation helper. No fixed market-accuracy percentage is claimed without real out-of-sample testing.

## Windows start

Extract the ZIP and double-click:

`Start Screener.bat`

The launcher creates `.venv`, installs requirements, copies `.env.example` to `.env` if needed, opens the browser, and starts the server on `http://127.0.0.1:8000`.

For exchange-live quote snapshots, add a valid Upstox Analytics Token to `.env` and set `USE_UPSTOX_LIVE=true`. Without it, Yahoo history remains an explicitly delayed fallback.

## Tests

```bash
pytest -q
```

The included tests cover indicator math, flat-RSI handling, score directionality, RVOL baseline correctness, HTF effects, timeframe architecture, dashboard order/arrows, and the 237→210 universe regression.
