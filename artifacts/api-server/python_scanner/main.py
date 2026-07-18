"""APEX Nifty 236 Scanner — FastAPI main entry point."""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from scanner_engine import ScannerEngine, get_market_status, scan_interval_secs, ist_now

# Configure logging
log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)
log_file = os.getenv("LOG_FILE", None)

handlers = [logging.StreamHandler()]
if log_file:
    handlers.append(logging.FileHandler(log_file))

logging.basicConfig(
    level=log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=handlers,
)
logger = logging.getLogger("apex")

engine = ScannerEngine()
_ws_clients: set[WebSocket] = set()


async def _push_to_all_clients(payload: dict) -> None:
    """Broadcast a JSON payload to all connected WS clients, pruning dead ones."""
    dead: list[WebSocket] = []
    for ws in list(_ws_clients):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


async def _bg_scan_loop() -> None:
    """
    Session-aware background scan loop.

    - 15m TF: every 1 minute
    - 1h, 4h, 1d TF: every 5 minutes
    - Outside market hours: all TFs every 60 minutes
    """
    logger.info("Background scan loop starting — running initial scan …")
    loop = asyncio.get_running_loop()

    # Initial scan on startup (always run regardless of session)
    try:
        await loop.run_in_executor(None, engine.run_all_scans, ["15m", "1h", "4h", "1d"])
        stats = engine.get_stats()
        await _push_to_all_clients({"type": "scan_complete", "stats": stats})
        logger.info(f"Initial scan complete. session={stats.get('session_status')} symbols={stats.get('total_symbols')}")
    except Exception as exc:
        logger.error(f"Initial scan error: {exc}")

    minute_counter = 0
    while True:
        ms = get_market_status()
        interval = 60
        if not ms["market_open"] and ms["session_status"] != "PRE_OPEN":
            interval = 3600

        logger.info(f"Next scan in {interval}s [session={ms['session_status']}]")
        await asyncio.sleep(interval)
        minute_counter += (interval // 60)

        timeframes = {"15m"}
        
        # Always scan timeframes that have active trades for 1-minute trailing tracking
        active_tfs = engine.get_active_timeframes()
        timeframes.update(active_tfs)

        if minute_counter % 5 == 0 or not ms["market_open"]:
            timeframes.update(["1h", "4h", "1d"])

        try:
            await loop.run_in_executor(None, engine.run_all_scans, list(timeframes))
            stats = engine.get_stats()
            # Push main scan_complete first
            await _push_to_all_clients({"type": "scan_complete", "stats": stats})
            # Push individual notification events (new signals, TP/SL hits)
            events = engine.pop_pending_events()
            for evt in events:
                await _push_to_all_clients(evt)
            if events:
                logger.info(f"Pushed {len(events)} notification event(s) to WS clients")
        except Exception as exc:
            logger.error(f"Periodic scan error: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_bg_scan_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="APEX Nifty 236 Scanner",
    description="Real-time algorithmic trading scanner for all 236 Nifty stocks",
    version="2.0.0",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/healthz")
@limiter.limit("60/minute")
def health_check(request: Request):
    stats = engine.get_stats()
    return {
        "status":    "ok",
        "version":   "2.0.0",
        "scanning":  stats["scanning"],
        "last_scan": stats["last_scan"],
    }


@app.get("/api/session")
@limiter.limit("30/minute")
def get_session(request: Request):
    """Current NSE market session status."""
    ms = get_market_status()
    return {
        **ms,
        "nse_open":  "09:15",
        "nse_close": "15:30",
        "exchange":  "NSE",
    }


@app.get("/api/signals")
@limiter.limit("30/minute")
def get_signals(
    request: Request,
    timeframe: Optional[str] = Query(None, description="Filter: 15m | 1h | 4h | 1d"),
    direction: Optional[str] = Query(None, description="Filter: BUY | SELL"),
):
    return engine.get_signals(timeframe=timeframe, direction=direction)


@app.get("/api/trades")
@limiter.limit("30/minute")
def get_trades(request: Request):
    return engine.get_active_trades()


@app.get("/api/leaderboard")
@limiter.limit("30/minute")
def get_leaderboard(
    request: Request,
    timeframe: Optional[str] = Query(None, description="Filter: 15m | 1h | 4h | 1d"),
):
    return engine.get_leaderboard(timeframe=timeframe)


@app.get("/api/chart/{symbol}/{timeframe}")
@limiter.limit("15/minute")
def get_chart(request: Request, symbol: str, timeframe: str):
    return engine.get_chart_data(symbol, timeframe)


@app.get("/api/stats")
@limiter.limit("30/minute")
def get_stats(request: Request):
    return engine.get_stats()


@app.get("/api/analytics")
@limiter.limit("30/minute")
def get_analytics(
    request: Request,
    tenure: Optional[str] = Query("30d", description="Filter tenure: 1d | 7d | 30d | 90d | 180d | 365d"),
):
    return engine.get_analytics(tenure=tenure)


@app.get("/api/symbols")
@limiter.limit("10/minute")
def get_symbols(request: Request):
    return engine.get_symbols()


@app.post("/api/scan")
@limiter.limit("5/minute")
async def trigger_scan(request: Request):
    """Trigger a fresh scan asynchronously."""
    loop = asyncio.get_running_loop()

    async def _run_and_push():
        await loop.run_in_executor(None, engine.run_all_scans)
        stats = engine.get_stats()
        await _push_to_all_clients({"type": "scan_complete", "stats": stats})

    asyncio.ensure_future(_run_and_push())
    return {"message": "Scan triggered", "scan_id": str(uuid.uuid4())}


@app.websocket("/api/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.add(websocket)
    # Send current state immediately on connect
    try:
        stats = engine.get_stats()
        await websocket.send_json({"type": "connected", "stats": stats})
    except Exception:
        pass
    try:
        while True:
            data = await websocket.receive_text()
            # Echo pings / handle client commands
            if data == "ping":
                await websocket.send_text("pong")
            elif data == "stats":
                await websocket.send_json({"type": "stats", "stats": engine.get_stats()})
    except WebSocketDisconnect:
        _ws_clients.discard(websocket)
    except Exception:
        _ws_clients.discard(websocket)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True, log_level="info")
