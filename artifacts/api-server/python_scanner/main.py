"""APEX Nifty 236 Scanner — FastAPI main entry point."""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect, Request, status, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyHeader
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from datetime import datetime

from scanner_engine import ScannerEngine, get_market_status, scan_interval_secs, ist_now
from nifty50 import TIMEFRAMES

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

    - 15m TF: every minute during the live NSE session
    - 1h, 4h, 1d TF: every 5 minutes during that session
    - Outside market hours: all TFs every 60 minutes
    """
    logger.info("Background scan loop starting — running initial scan …")
    loop = asyncio.get_running_loop()

    # Initial scan on startup (always run regardless of session)
    for attempt in range(3):
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, engine.run_all_scans, ["15m", "1h", "4h", "1d"]),
                timeout=360.0,
            )
            stats = engine.get_stats()
            await _push_to_all_clients({"type": "scan_complete", "stats": stats})
            logger.info(f"Initial scan complete. session={stats.get('session_status')} symbols={stats.get('total_symbols')}")
            break
        except Exception as exc:
            logger.error(f"Initial scan attempt {attempt+1} error: {exc}")
            await asyncio.sleep(2.0 * (attempt + 1))

    live_scan_counter = 0
    while True:
        # Read the session immediately before sleeping only to choose the wait.
        # It must be read again afterwards; otherwise a 09:14/15:30 boundary
        # is processed using stale session data.
        interval = scan_interval_secs()

        logger.info(f"Next scan in {interval}s [session={get_market_status()['session_status']}]")
        await asyncio.sleep(interval)
        ms = get_market_status()

        if ms["market_open"]:
            live_scan_counter += 1
            # Do not expand each one-minute scan with active higher timeframes:
            # that made every cycle take several minutes and delayed the next
            # live 15m refresh. Higher-timeframe trailing is handled below.
            timeframes = {"15m"}
            if live_scan_counter % 5 == 0:
                timeframes.update(["1h", "4h", "1d"])
        else:
            live_scan_counter = 0
            timeframes = {"15m", "1h", "4h", "1d"}

        for attempt in range(2):
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, engine.run_all_scans, list(timeframes)),
                    timeout=300.0,
                )
                stats = engine.get_stats()
                # Push main scan_complete first
                await _push_to_all_clients({"type": "scan_complete", "stats": stats})
                # Push individual notification events (new signals, TP/SL hits)
                events = engine.pop_pending_events()
                for evt in events:
                    await _push_to_all_clients(evt)
                if events:
                    logger.info(f"Pushed {len(events)} notification event(s) to WS clients")
                break
            except Exception as exc:
                logger.error(f"Periodic scan attempt {attempt+1} error: {exc}")
                await asyncio.sleep(1.5 * (attempt + 1))


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_bg_scan_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        engine.shutdown()


limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="APEX Nifty 236 Scanner",
    description="Real-time algorithmic trading scanner for all 236 Nifty stocks",
    version="2.0.0",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

allowed_origins = [
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:3000",
]
if os.getenv("ALLOWED_ORIGINS"):
    allowed_origins.extend([o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()])

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _check_auth(request: Request) -> None:
    """Enforce API Key authentication if APEX_API_KEY is configured in environment."""
    required_key = os.getenv("APEX_API_KEY", "").strip()
    if not required_key:
        return
    auth_header = request.headers.get("Authorization", "")
    api_key_header = request.headers.get("X-API-Key", "")
    if auth_header.startswith("Bearer ") and auth_header[7:].strip() == required_key:
        return
    if api_key_header.strip() == required_key:
        return
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")


def _validate_timeframe(timeframe: Optional[str]) -> Optional[JSONResponse]:
    """422 for unknown timeframes instead of silently ignoring the filter."""
    if timeframe is not None and timeframe not in TIMEFRAMES:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": f"Unknown timeframe {timeframe!r}; valid: {TIMEFRAMES}"},
        )
    return None


@app.get("/api/healthz")
@limiter.limit("60/minute")
def health_check(request: Request):
    """Health with real degradation states (previously always 'ok')."""
    stats = engine.get_stats()
    problems: list[str] = []

    last_scan = stats.get("last_scan")
    if last_scan:
        try:
            age = (ist_now() - datetime.fromisoformat(last_scan)).total_seconds()
            # 3 live cycles (~60s interval + latency) or 3h off-market.
            limit = 600 if stats.get("market_open") else 10800
            if age > limit:
                problems.append(f"last scan is {age:.0f}s old")
        except Exception:
            pass
    elif stats.get("scan_count", 0) > 0 and not stats.get("scanning"):
        problems.append("no completed scan yet")

    if stats.get("scan_errors", 0) > 0:
        problems.append(f"{stats['scan_errors']} symbol errors in last scan")

    data_health = stats.get("data_health") or {}
    feed = (data_health.get("feed") or {}) if isinstance(data_health, dict) else {}
    if stats.get("market_open") and data_health.get("angel_active"):
        packet_age = feed.get("last_packet_age_s")
        if packet_age is not None and packet_age > 120:
            problems.append(f"tick feed silent for {packet_age:.0f}s")
        yahoo_count = data_health.get("source_by_symbol_yahoo") or 0
        if yahoo_count > 0:
            problems.append(f"{yahoo_count} symbols on delayed Yahoo fallback")

    return {
        "status":      "degraded" if problems else "ok",
        "problems":    problems,
        "version":     "2.1.0",
        "scanning":    stats["scanning"],
        "last_scan":   stats["last_scan"],
        "data_health": data_health,
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
    invalid = _validate_timeframe(timeframe)
    if invalid:
        return invalid
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
    invalid = _validate_timeframe(timeframe)
    if invalid:
        return invalid
    return engine.get_leaderboard(timeframe=timeframe)


# 60/min: the dashboard polls a cold chart every ~2.5s until candles arrive;
# the old 15/min limit made the UI rate-limit itself into an error state.
@app.get("/api/chart/{symbol}/{timeframe}")
@limiter.limit("60/minute")
def get_chart(request: Request, symbol: str, timeframe: str):
    invalid = _validate_timeframe(timeframe)
    if invalid:
        return invalid
    return Response(
        content=engine.get_chart_json(symbol, timeframe),
        media_type="application/json",
    )


@app.get("/api/stats")
@limiter.limit("30/minute")
def get_stats(request: Request):
    return engine.get_stats()


@app.get("/api/analytics", dependencies=[Depends(_check_auth)])
@limiter.limit("30/minute")
def get_analytics(
    request: Request,
    tenure: Optional[str] = Query("30d", description="Filter tenure: 1d | 7d | 30d | 90d | 180d | 365d"),
):
    valid_tenures = {"1d", "7d", "30d", "90d", "180d", "365d"}
    if tenure not in valid_tenures:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": f"Invalid tenure {tenure!r}; valid choices: {sorted(valid_tenures)}"},
        )
    return engine.get_analytics(tenure=tenure)


@app.get("/api/symbols")
@limiter.limit("10/minute")
def get_symbols(request: Request):
    return engine.get_symbols()


@app.get("/api/settings", dependencies=[Depends(_check_auth)])
@limiter.limit("30/minute")
def get_settings_endpoint(request: Request):
    from settings_store import get_settings
    return get_settings()


@app.post("/api/settings", dependencies=[Depends(_check_auth)])
@limiter.limit("10/minute")
async def update_settings_endpoint(request: Request):
    """Save user scanner settings; applied from the next scan onward."""
    from settings_store import update_settings
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Body must be a JSON object")
        merged = update_settings(body)
    except ValueError as exc:
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)})
    # Refresh results with the new parameters without waiting for the cycle.
    if not engine.get_stats()["scanning"]:
        loop = asyncio.get_running_loop()
        asyncio.ensure_future(loop.run_in_executor(None, engine.run_all_scans))
    return {"settings": merged, "applied": "next scan (triggered now if idle)"}


@app.get("/api/history")
@limiter.limit("30/minute")
def get_history(request: Request, limit: int = Query(300, ge=1, le=1000)):
    """Closed-trade log (exits, stop-loss, target hits) for the History tab."""
    return engine.get_history(limit=limit)


@app.post("/api/scan", dependencies=[Depends(_check_auth)])
@limiter.limit("5/minute")
async def trigger_scan(request: Request):
    """Trigger a fresh scan asynchronously (honest about overlap skips)."""
    if engine.get_stats()["scanning"]:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"message": "Scan already running — request skipped", "started": False},
        )

    loop = asyncio.get_running_loop()

    async def _run_and_push():
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, engine.run_all_scans),
                timeout=300.0,
            )
            stats = engine.get_stats()
            await _push_to_all_clients({"type": "scan_complete", "stats": stats})
        except Exception as exc:
            logger.error(f"Manual scan failed: {exc}")

    asyncio.ensure_future(_run_and_push())
    return {"message": "Scan triggered", "started": True, "scan_id": str(uuid.uuid4())}


@app.get("/api/brokers/status")
@limiter.limit("30/minute")
def get_brokers_status(request: Request):
    """Multi-broker load balancer status, rate limits, and Upstox/Angel health."""
    from broker_dispatcher import get_dispatcher
    from data_provider import get_data_health
    return {
        "dispatcher": get_dispatcher().status(),
        "data_health": get_data_health(),
    }


@app.get("/api/options/resolve")
@limiter.limit("30/minute")
def get_resolved_option(
    request: Request,
    symbol: str = Query(..., description="Underlying symbol or index (e.g., ^NSEI, RELIANCE.NS)"),
    spot_price: float = Query(..., gt=0, description="Current underlying spot price"),
    direction: str = Query("LONG", description="LONG/BUY for CE, SHORT/SELL for PE"),
    strike_offset: int = Query(0, description="Strike offset: 0=ATM, 1=OTM1, -1=ITM1"),
):
    """Dynamic resolution of active ATM/OTM/ITM NSE_FO option contracts via Upstox master."""
    from options_engine import resolve_atm_option
    contract = resolve_atm_option(symbol, spot_price, direction, strike_offset=strike_offset)
    if not contract:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"message": f"No active option contract found for {symbol} @ {spot_price}"},
        )
    return contract


@app.post("/api/options/trade", dependencies=[Depends(_check_auth)])
@limiter.limit("10/minute")
async def post_option_trade(request: Request):
    """Execute live option entry (CE/PE) directly via Upstox MultiBrokerDispatcher."""
    from options_engine import execute_option_trade
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Body must be a JSON object")
        required = ["underlying_symbol", "spot_price", "direction", "quantity", "stop_spot", "target_spot"]
        for k in required:
            if k not in body:
                raise ValueError(f"Missing required parameter: {k!r}")
        res = execute_option_trade(
            underlying_symbol=str(body["underlying_symbol"]),
            spot_price=float(body["spot_price"]),
            direction=str(body["direction"]),
            quantity=int(body["quantity"]),
            stop_spot=float(body["stop_spot"]),
            target_spot=float(body["target_spot"]),
            timeframe=str(body.get("timeframe", "15m")),
            stop_mode=str(body.get("stop_mode", "Delta-Translated")),
            tag=str(body.get("tag", "")),
        )
        if not res["status"]:
            return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content=res)
        return res
    except ValueError as exc:
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)})


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
    # The desktop launcher owns process restarts.  Uvicorn's reload supervisor
    # creates a parent/child pair on Windows, which left a supervisor alive after
    # the launcher killed the port-owning child.  The supervisor then respawned
    # the child and caused the next launch to fail with WinError 10013.
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
