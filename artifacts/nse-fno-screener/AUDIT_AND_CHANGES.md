# Audit & Rebuild Notes

## Critical issues found

1. `scheduler._refresh_group()` was a placeholder and never called the scoring engine or any market-data source.
2. The scheduler itself was never started by FastAPI, so `LATEST_SCANS` could remain empty forever.
3. The Upstox adapter had `connect()` and `get_bars()` as `NotImplementedError` stubs.
4. No frontend was included in the ZIP despite the API/WebSocket comments referring to one.
5. The bundled NSE F&O universe was static; every `upstox_instrument_key` was null, so it could not support live Upstox quotes.
6. NSE holiday handling was an empty placeholder, so holidays could be mislabeled as LIVE.
7. Yahoo calls were per-symbol and synchronous in the async interface, which does not scale to a 200+ symbol universe.
8. No hard stale-data guard existed. A connected app could display old bars without an explicit STALE state.
9. No Windows one-click launcher/start button existed.
10. No manual force-refresh endpoint/button existed.

## Changes made

- Added daily NSE F&O universe synchronization from Upstox's NSE BOD JSON master, retaining the last-known-good or bundled list on failure.
- Reworked Yahoo fallback into bulk threaded downloads.
- Added a real scan pipeline for all 8 timeframes.
- Added explicit `LIVE`, `DELAYED`, `STALE`, and `OFFLINE` handling based on source/bar age.
- Added verified 2026 NSE F&O trading holidays and IST market-session handling.
- Added optional Upstox full-market-quote overlay (one request supports up to 500 instruments) using a read-only Analytics Token.
- Added FastAPI lifespan startup for scheduler loops.
- Added REST all-scans/health/manual-refresh endpoints and WebSocket snapshot/update push.
- Added a new responsive dark Windows-webapp dashboard with timeframe tabs, freshness badges, scan metadata, signal reasons, and Refresh Now button.
- Added `start.bat` and `Start Screener.bat`; they create the venv, install dependencies, create `.env`, open the browser, and start the server.
- Preserved closed-bar scoring so an unfinished candle is not silently treated as completed.

## Important freshness truth

Without an exchange-authorized live source, no application can guarantee real-time NSE prices. Yahoo Finance remains a delayed fallback and is labelled as such. With `USE_UPSTOX_LIVE=true` and a valid `UPSTOX_ANALYTICS_TOKEN`, the dashboard overlays exchange-sourced Upstox live quote snapshots while technical scores remain based on closed Yahoo bars and continue to expose their bar freshness. The app never relabels delayed bars as live.

## v4 refresh architecture — 2026-09-03

Reworked the scheduler after the requirement that 5m/15m/30m remain near-continuous without starving 1h/4h/macro timeframes.

- Added 30-minute timeframe end-to-end.
- Removed the single global scheduler lock that allowed slow scans to block fast scans.
- Added four independent lanes:
  - Quote pulse: 15s live-market target, one bulk request for the whole universe.
  - Micro scan: 5m/15m/30m every 55s.
  - Medium scan: 1h/4h every 120s.
  - Macro scan: 1d/2d/4d/1w every 300s.
- Loops start staggered but run concurrently afterwards.
- Added closed-market slower cadences to avoid wasting requests when no new NSE ticks/bars exist.
- Added per-timeframe locks instead of a global lock.
- Added a cached Upstox bulk-quote layer with a hard anti-burst guard and request stats.
- Fixed Full Market Quote last-trade timestamp handling (`last_trade_time`).
- Quote pulses update displayed prices between technical rescans without altering closed-bar indicator scores.
- Added scheduler diagnostics to `/api/health`.
- Added manual-refresh cooldown protection.

Important: technical scores intentionally use CLOSED candles. A faster live-price pulse does not turn a still-forming 5-minute candle into a closed candle; doing so would reintroduce repainting. The design therefore separates live quote freshness from closed-bar signal freshness.
- Micro history is now shared: 5m, 15m and 30m all reuse ONE cached 5-minute universe download; 15m/30m are resampled locally and sealed. This removes the extra 15-minute universe download from every micro cycle.
- Scheduler cadences are start-to-start targets, not "sleep N seconds after work finishes". Scan runtime is subtracted from the sleep time.
- CPU-heavy pandas scoring is offloaded from the asyncio event loop so quote pulses and macro scans stay responsive.

## Dashboard v3 — Split signal boards + intraday MTF matrix

- Replaced the single mixed signal table with independent **Bullish Signals** and **Bearish Signals** tables.
- Added per-symbol signal columns in fixed order: **5m, 15m, 30m, 1h, 4h**.
- Each timeframe cell is computed from that timeframe's own closed-bar score, so disagreement between timeframes is visible instead of copying the selected timeframe signal.
- Directional cells use **▲ green** for bullish/buying, **▼ red** for bearish/selling, and **→ neutral** for wait/mixed conditions.
- 1D/7D/30D percentage cells use their own sign for arrows/colors; RSI uses its own 50-line direction.
- ADX and RVOL are directionless measures, so their arrow/color inherits the row's current selected-timeframe direction while retaining the actual numeric value.
- Added `/api/intraday-matrix`, a local-memory-only endpoint. It never makes a provider request; it reads compact full-universe snapshots produced by the existing scan lanes.
- Full-universe MTF snapshots are stored separately from WebSocket scan payloads so the 15-second quote pulse remains lightweight.
- MTF cells refresh after their corresponding scan generation changes; quote-only pulses do not trigger redundant MTF fetches.

## 2026-09-03 — 237-universe + scoring v3 audit

### Universe bug: 237 became ~210
Root cause: the old daily Upstox BOD sync generated a *new* universe from futures/equity matches. Any symbol that did not match both sides disappeared from the screener, even though `data/instruments.json` contained the user's full 237-symbol list.

Fix: the bundled 237-symbol list is now authoritative. Upstox BOD data only enriches each existing row with live instrument keys, nearest-future key/expiry and updated lot size. A failed match is reported as metadata and the symbol remains in the universe. Old same-day 210-row caches are rejected. `data/nifty_fno_237.txt` is included as the supplied reference list.

The scanner response now also exposes `failed_symbols` so `universe_count` and `scored_count` cannot be confused. `TOP_ROWS` defaults to 237 so the UI does not silently truncate a side to 40 rows.

### Dashboard column correction
New order: Symbol, Price, Score, Signal, 5m, 15m, 30m, 1h, 4h, 1D%, 2D%, 4D%, 7D%, 30D%, RSI, ADX, RVOL, Freshness, Bar time, Why.

2D and 4D returns are now computed by the backend from daily closed bars; they are not UI placeholders.

### Scoring-engine defects found
1. HTF confirmation existed in `score_symbol_timeframe()` but the pipeline never supplied it. The weight was therefore dead. Pipeline now reads the next slower timeframe's latest non-stale trend alignment and passes it into the score.
2. RSI mapped flat price history to 100 because all NaN states were filled with 100. Flat markets now correctly resolve to RSI 50; gains-only to 100 and losses-only to 0.
3. RVOL used a mean containing the current bar, diluting its own spike. Current volume now compares against the previous 20 bars only.
4. The old score relied heavily on correlated direction terms and coarse EMA signs. v3 separates EMA stack, EMA slope, short/medium momentum, MACD level/acceleration, RSI, structure, DMI direction and prior-range breakout/breakdown.
5. ADX remains strength-only and cannot create direction. DMI supplies signed directional information.
6. A factor-agreement multiplier dampens high scores when meaningful factors conflict.
7. Volume confirmation is smooth and bounded so a huge volume print cannot force many symbols to +/-100 and destroy ranking resolution.
8. Pipeline thresholds now use the active timeframe profile's thresholds instead of hard-coded +/-20 values.

### Accuracy statement
Logic correctness and invariants are unit-tested, but no fixed market "accuracy" percentage is claimed. Market accuracy requires a defined forward-return target, walk-forward/out-of-sample data and realistic costs. `app/engine/scoring_validation.py` provides a no-lookahead walk-forward directional evaluation helper for real closed OHLCV history.
