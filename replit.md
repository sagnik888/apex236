# APEX Nifty 50 Trading Dashboard

A real-time algorithmic trading scanner for all 50 Nifty stocks across 15m, 1h, 4h and 1d timeframes. Runs the APEX Hybrid Pro scanner engine, pulls 15-min delayed data from Yahoo Finance, generates bidirectional BUY/SELL signals with targets and stop-losses, tracks trades until exit, and displays everything in a dark-mode terminal dashboard with TradingView Lightweight Charts.

## Run & Operate

- **Frontend:** `pnpm --filter @workspace/trading-dashboard run dev` (port auto-assigned, workflow: `artifacts/trading-dashboard: web`)
- **Backend:** `bash -c 'cd /home/runner/workspace/artifacts/api-server/python_scanner && python main.py'` (port 8080, workflow: `artifacts/api-server: API Server`)
- `pnpm run typecheck` — full TypeScript typecheck across all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec

## Stack

- **Frontend:** React + Vite, TailwindCSS, TanStack Query, TradingView Lightweight Charts v5.2, Wouter router
- **Backend:** Python 3.13 + FastAPI + Uvicorn (replaces the Node.js Express server)
- **Scanner:** APEX Hybrid Pro v5.5 (Python translation) — `artifacts/api-server/python_scanner/apex_python_scanner.py`
- **Data:** Yahoo Finance via `yfinance` (15-min delayed), scanned on startup then every 5 minutes
- **API codegen:** Orval (from `lib/api-spec/openapi.yaml` → React Query hooks in `lib/api-client-react`)

## Where things live

- `artifacts/api-server/python_scanner/` — Python FastAPI backend + APEX scanner
  - `main.py` — FastAPI entry point, WebSocket, background scan loop
  - `scanner_engine.py` — multi-symbol, multi-timeframe scan orchestration
  - `data_provider.py` — Yahoo Finance OHLCV fetcher (15m/1h/4h/1d)
  - `nifty50.py` — Nifty 50 symbol list
  - `apex_python_scanner.py` — APEX scanner core (do not edit)
- `artifacts/trading-dashboard/src/` — React frontend
- `lib/api-spec/openapi.yaml` — OpenAPI contract (source of truth for TS types)
- `lib/api-client-react/src/generated/` — auto-generated React Query hooks

## Architecture decisions

- **Python backend replaces Node.js:** The APEX scanner is pure Python/pandas. FastAPI on port 8080 serves `/api/*`. The Node.js build remains but dev command is overridden in `artifact.toml`.
- **4h timeframe via resampling:** Yahoo Finance has no native 4h interval; we fetch 1h and resample with pandas.
- **Per-timeframe ApexConfig:** Each of the 4 timeframes uses a slightly different min_score/conflict_margin tuned for intraday vs swing.
- **Scanner runs in ThreadPoolExecutor:** 10 concurrent workers fetch+scan all 49×4=196 combinations. Initial scan takes ~2-3 minutes.
- **No DB:** All scan results held in memory (ScannerEngine). Stateless across restarts — first scan re-populates everything.

## Product

- **Scanner page (`/`):** Signal table for all Nifty 50 stocks with timeframe/direction filters, RSI, ADX, bias, SL/TP, state, live P&L
- **Trades page (`/trades`):** Active and pending trade tracker with entry vs current price, target milestone badges (T1/T2/T3)
- **Chart page (`/chart/:symbol/:timeframe`):** Candlestick chart with BUY/SELL signal markers and SL/TP price lines

## Gotchas

- **Python packages installed via uv/Nix** — do NOT use `pip install`. Use the `installLanguagePackages` callback or `uv add` in the workspace.
- **Scan takes 2-3 minutes on first startup** — the UI shows "Scanning..." and an empty table until results arrive.
- **`artifact.toml` dev command** uses absolute path `/home/runner/workspace/artifacts/api-server/python_scanner` — relative paths fail in the workflow runner.
- **After each OpenAPI spec change**, run `pnpm --filter @workspace/api-spec run codegen` before using updated types.

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
