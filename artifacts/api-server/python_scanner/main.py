"""APEX Nifty 236 Scanner — FastAPI main entry point."""
from __future__ import annotations

import asyncio
import json
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
from concurrent.futures import ThreadPoolExecutor

scanner_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bg-scanner")
# OCO reconciliation must NOT share the scan pool. That pool has exactly one
# worker and a scan cycle takes seconds to tens of seconds, so reconciliation
# queued behind it — meaning a filled stop's sibling target could stay live for
# the length of a full scan. Order safety gets its own thread.
oms_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="oms")

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


# A slow or half-open websocket client must never hold up the scan loop.
_WS_SEND_TIMEOUT = 5.0
_WS_MAX_CLIENTS = 64


async def _push_to_all_clients(payload: dict) -> None:
    """Broadcast to all connected WS clients concurrently, pruning dead ones.

    This used to `await ws.send_json(...)` sequentially with no timeout, from
    inside _bg_scan_loop. One client on a stalled TCP connection blocked the
    broadcast, and therefore the whole scan cycle, indefinitely. Sending
    concurrently with a per-client timeout bounds the cost at _WS_SEND_TIMEOUT
    no matter how many clients are wedged.
    """
    clients = list(_ws_clients)
    if not clients:
        return

    async def send(ws) -> bool:
        """True if delivered; False if the client timed out or errored."""
        try:
            await asyncio.wait_for(ws.send_json(payload), timeout=_WS_SEND_TIMEOUT)
            return True
        except asyncio.TimeoutError:
            logger.warning("WS client exceeded %.0fs send timeout; dropping it.", _WS_SEND_TIMEOUT)
            return False
        except Exception as exc:
            logger.debug("WS send failed: %s", exc)
            return False

    # Results are positional, so failures map back by INDEX. Identifying them by
    # isinstance() is fragile — it silently stops pruning anything the type check
    # does not recognise, which is exactly the wedged-client case that matters.
    results = await asyncio.gather(*(send(ws) for ws in clients), return_exceptions=True)
    for ws, ok in zip(clients, results):
        if ok is not True:
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
                loop.run_in_executor(scanner_pool, engine.run_all_scans, ["15m", "1h", "4h", "1d"]),
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
        try:
            # Read the session immediately before sleeping only to choose the wait.
            # It must be read again afterwards; otherwise a 09:14/15:30 boundary
            # is processed using stale session data.
            interval = scan_interval_secs()

            logger.info(f"Next scan in {interval}s [session={get_market_status()['session_status']}]")
            await asyncio.sleep(interval)
            ms = get_market_status()

            # Reconcile any open option OCO pairs (cancel the sibling of a filled
            # leg) before the next scan so no stale protective/target order lingers.
            try:
                from options_engine import reconcile_open_ocos
                # Own pool + hard timeout: a hung broker call must not stall
                # the scan loop, and reconciliation must not queue behind a scan.
                oco_actions = await asyncio.wait_for(
                    loop.run_in_executor(oms_pool, reconcile_open_ocos), timeout=30.0
                )
                if oco_actions:
                    logger.info(f"OCO reconcile: {oco_actions}")
            except Exception as exc:
                logger.debug(f"OCO reconcile skipped: {exc}")

            # Auto square-off on expiry day at 15:20 IST
            try:
                from options_engine import should_auto_squareoff, auto_squareoff_positions
                if should_auto_squareoff():
                    squareoff_result = await asyncio.wait_for(
                        loop.run_in_executor(oms_pool, auto_squareoff_positions), timeout=30.0
                    )
                    if squareoff_result:
                        logger.info(f"Expiry squareoff: {squareoff_result}")
            except Exception as exc:
                logger.debug(f"Expiry squareoff check skipped: {exc}")

            if ms["market_open"]:
                live_scan_counter += 1
                # Staggered cadence: 15m every cycle, 1h every 5, 4h every 10,
                # 1d every 15. Previously all three higher timeframes landed on
                # the same 5th cycle, so one cycle in five carried 4x the work
                # and delayed the next live 15m refresh.
                from scanner_engine import timeframes_due
                timeframes = set(timeframes_due(live_scan_counter))
            else:
                live_scan_counter = 0
                timeframes = {"15m", "1h", "4h", "1d"}

            for attempt in range(2):
                try:
                    await asyncio.wait_for(
                        loop.run_in_executor(scanner_pool, engine.run_all_scans, list(timeframes)),
                        timeout=300.0,
                    )
                    stats = engine.get_stats()
                    # Push main scan_complete first
                    await _push_to_all_clients({"type": "scan_complete", "stats": stats})
                    # Push individual notification events (new signals, TP/SL hits)
                    events = engine.pop_pending_events()

                    # Route state transitions to the broker BEFORE notifying the
                    # UI: order placement is the time-critical half. Runs on the
                    # OMS pool so a slow broker call cannot stall the scan loop,
                    # and is fully gated by is_live_execution().
                    if events:
                        try:
                            from oms import get_oms
                            summary = await asyncio.wait_for(
                                loop.run_in_executor(oms_pool, get_oms().handle_events, events),
                                timeout=60.0,
                            )
                            if summary.get("opened") or summary.get("closed"):
                                logger.info("OMS: %s", summary)
                        except Exception as exc:
                            logger.error("OMS dispatch failed: %s", exc)

                    for evt in events:
                        await _push_to_all_clients(evt)
                    if events:
                        logger.info(f"Pushed {len(events)} notification event(s) to WS clients")
                    break
                except Exception as exc:
                    logger.error(f"Periodic scan attempt {attempt+1} error: {exc}")
                    await asyncio.sleep(1.5 * (attempt + 1))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Fatal error in background scan loop: {exc}")
            await asyncio.sleep(5.0)


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
        scanner_pool.shutdown(wait=False, cancel_futures=True)
        oms_pool.shutdown(wait=False, cancel_futures=True)
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


def _check_csrf(request: Request) -> None:
    """Block cross-site writes that never trigger a CORS preflight.

    CORSMiddleware only protects a route when the browser sends a preflight, and
    a preflight is only sent for "non-simple" requests. Starlette's
    `Request.json()` never inspects Content-Type, so a cross-origin
    `fetch(..., {mode:'no-cors', headers:{'Content-Type':'text/plain'}, body:'{...}'})`
    is a CORS *simple request*: no preflight, the handler runs, the write lands.
    Only the response is hidden from the attacking page — which is irrelevant
    when the goal is to mutate risk settings.

    Two independent gates, both cheap:
      1. Require a real JSON Content-Type, which forces a preflight.
      2. Require any Origin header present to be on the allowlist. CORSMiddleware
         does not enforce this itself for simple requests.
    Non-browser clients (curl, python) send no Origin and are unaffected here —
    they are what APEX_API_KEY is for.
    """
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Content-Type must be application/json",
        )
    origin = request.headers.get("origin")
    if origin and origin not in allowed_origins:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-origin write rejected",
        )


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


@app.post("/api/settings", dependencies=[Depends(_check_auth), Depends(_check_csrf)])
@limiter.limit("10/minute")
async def update_settings_endpoint(request: Request):
    """Save user scanner settings; applied from the next scan onward."""
    from settings_store import update_settings
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Body must be a JSON object")
        loop = asyncio.get_running_loop()
        merged = await loop.run_in_executor(None, update_settings, body)
    except Exception as exc:
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)})
    # Refresh results with the new parameters without waiting for the cycle.
    if not engine.get_stats()["scanning"]:
        loop = asyncio.get_running_loop()
        asyncio.ensure_future(loop.run_in_executor(scanner_pool, engine.run_all_scans))
    return {"settings": merged, "applied": "next scan (triggered now if idle)"}


@app.get("/api/history")
@limiter.limit("30/minute")
def get_history(request: Request, limit: int = Query(300, ge=1, le=1000), symbol: Optional[str] = Query(None, description="Filter by symbol (e.g. INDIANB)"), timeframe: Optional[str] = Query(None, description="Filter by timeframe (e.g. 15m, 1h, 4h, 1d)")):
    """Closed-trade log (exits, stop-loss, target hits) for the History tab."""
    return engine.get_history(limit=limit, symbol=symbol, timeframe=timeframe)


@app.post("/api/scan", dependencies=[Depends(_check_auth), Depends(_check_csrf)])
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
                loop.run_in_executor(scanner_pool, engine.run_all_scans),
                timeout=300.0,
            )
            stats = engine.get_stats()
            await _push_to_all_clients({"type": "scan_complete", "stats": stats})
        except Exception as exc:
            logger.error(f"Manual scan failed: {exc}")

    asyncio.ensure_future(_run_and_push())
    return {"message": "Scan triggered", "started": True, "scan_id": str(uuid.uuid4())}


@app.get("/api/brokers/status")
# Polled by SystemHealthPanel (10s) AND Dashboard (10s) = 12 req/min per
# browser tab, and slowapi keys on remote address, so tabs share the
# budget: three open tabs would 429 against a 30/min limit. The handler
# is ~1ms (no network I/O), so the cost of a higher ceiling is nil.
@limiter.limit("120/minute")
def get_brokers_status(request: Request):
    """Multi-broker load balancer status, rate limits, and Upstox/Angel health."""
    from broker_dispatcher import get_dispatcher
    from data_provider import get_data_health
    return {
        "dispatcher": get_dispatcher().status(),
        "data_health": get_data_health(),
    }


@app.get("/api/auth/upstox", dependencies=[Depends(_check_auth)])
@limiter.limit("60/minute")
def get_upstox_auth_status(request: Request):
    """Upstox session state + the browser URL to start a new one.

    Upstox tokens die at 03:30 IST every day and cannot be renewed
    programmatically — the exchange step needs a human browser login. Surfacing
    the countdown lets the operator re-auth BEFORE the session lapses instead of
    discovering it when an order fails.
    """
    from upstox_auth import get_upstox_auth

    auth = get_upstox_auth()
    payload = auth.status()
    try:
        payload["login_url"] = auth.login_url()
    except ValueError as exc:
        payload["login_url"] = None
        payload["reason"] = payload.get("reason") or str(exc)
    return payload


@app.post("/api/auth/upstox", dependencies=[Depends(_check_auth), Depends(_check_csrf)])
@limiter.limit("10/minute")
async def complete_upstox_login(request: Request):
    """Finish the OAuth flow. Accepts the bare code or the whole redirect URL."""
    from upstox_auth import get_upstox_auth

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Body must be JSON")
    code = str((body or {}).get("code") or (body or {}).get("url") or "").strip()
    if not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Supply the authorization code or the redirect URL as 'code'")
    try:
        result = get_upstox_auth().complete_login(code)
    except Exception as exc:
        # The operator needs Upstox's actual complaint, not a generic 500.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return {"status": "ok", **result}


@app.post("/api/auth/upstox/recheck", dependencies=[Depends(_check_auth), Depends(_check_csrf)])
@limiter.limit("30/minute")
def recheck_upstox_session(request: Request):
    """Re-probe the cached token (after a manual edit of upstox_secrets.env)."""
    from upstox_auth import get_upstox_auth

    auth = get_upstox_auth()
    auth.load_cached_session()
    return auth.status()


@app.get("/api/indices")
@limiter.limit("60/minute")
def get_indices(request: Request):
    """NSE index tiers available for the scan-universe toggle.

    `scannable` is what the tier contributes to the live scan; it can be lower
    than `total` when a constituent is not in the configured universe (e.g.
    TMCV after the Tata Motors split). Reported rather than hidden so index
    drift after an NSE rebalance is visible.
    """
    from index_classification import coverage_report, describe
    from scanner_engine import SCAN_SYMBOLS, active_scan_symbols
    from settings_store import get_settings

    report = coverage_report()
    return {
        "indices": describe(),
        "selected": get_settings().get("enabled_indices"),
        "active_symbols": len(active_scan_symbols()),
        "universe_symbols": len(SCAN_SYMBOLS),
        "unclassified_in_universe": report["unclassified_in_universe"],
        "classified_not_in_universe": report["classified_not_in_universe"],
    }


@app.post("/api/webhooks/{broker}")
@limiter.limit("600/minute")
async def broker_order_webhook(request: Request, broker: str):
    """Broker order-update intake (Upstox postback / Angel order push).

    The system had no webhook integration at all: order state was polled at most
    once per 60-second scan and lost on restart, leaving a multi-minute window
    between a stop filling and its sibling target being cancelled.

    Authenticated with APEX_WEBHOOK_SECRET (query param `secret` or the
    X-Apex-Signature header) and FAILS CLOSED when that is unset — a forged fill
    can drive the OMS into cancelling or reversing a real position.
    """
    from order_events import ingest, verify_webhook_secret, webhook_secret_configured

    if not webhook_secret_configured():
        logger.error("Rejected %s webhook: APEX_WEBHOOK_SECRET is not configured", broker)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook intake disabled: APEX_WEBHOOK_SECRET is not configured",
        )
    supplied = request.headers.get("X-Apex-Signature") or request.query_params.get("secret")
    if not verify_webhook_secret(supplied):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook secret")

    if broker.lower() not in ("upstox", "angel"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown broker {broker!r}")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Body must be JSON")

    event = ingest(broker, payload if isinstance(payload, dict) else {})
    if event is None:
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED,
                            content={"accepted": False, "reason": "unparseable payload"})

    # A terminal leg must cancel its sibling immediately, not at the next poll.
    try:
        from options_engine import on_order_event
        actions = on_order_event(event)
    except Exception as exc:
        logger.error("OCO reaction to %s failed: %s", event.order_id, exc)
        actions = []
    return {"accepted": True, "order_id": event.order_id, "status": event.status, "actions": actions}


@app.get("/api/oms/positions", dependencies=[Depends(_check_auth)])
@limiter.limit("60/minute")
def get_oms_positions(request: Request):
    """Positions the OMS believes the broker holds, plus recent order intents.

    Distinct from /api/trades, which is the scanner's simulated book. A gap
    between the two is exactly the live-vs-simulated divergence the audit found
    nobody could see.
    """
    from oms import get_oms
    from simulation_engine import get_execution_mode, is_live_execution

    oms = get_oms()
    return {
        "execution_mode": get_execution_mode(),
        "live": is_live_execution(),
        "positions": oms.positions(),
        "pending_stabilisation": oms.pending(),
        "recent_intents": oms.intents(50),
    }


@app.get("/api/orders/events", dependencies=[Depends(_check_auth)])
@limiter.limit("60/minute")
def get_order_events(request: Request, limit: int = Query(100, ge=1, le=500)):
    """Recent broker order updates, newest first."""
    from order_events import get_store, webhook_secret_configured
    return {
        "webhook_enabled": webhook_secret_configured(),
        "events": get_store().recent(limit),
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


@app.post("/api/options/trade", dependencies=[Depends(_check_auth), Depends(_check_csrf)])
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

@app.get("/api/options/chain")
@limiter.limit("30/minute")
def get_options_chain_endpoint(
    request: Request,
    symbol: str = Query(..., description="Underlying symbol"),
    expiry: Optional[str] = Query(None, description="Expiry date YYYY-MM-DD")
):
    from data_provider import fetch_options_chain
    chain = fetch_options_chain(symbol, expiry)
    return {"symbol": symbol, "chain": chain}

@app.get("/api/options/greeks")
@limiter.limit("60/minute")
def get_options_greeks_endpoint(
    request: Request,
    instrument_key: str = Query(...)
):
    from data_provider import fetch_option_greeks
    return fetch_option_greeks(instrument_key)

@app.get("/api/daybook", dependencies=[Depends(_check_auth)])
@limiter.limit("30/minute")
def get_daybook(request: Request):
    try:
        from broker_upstox import get_upstox_client
        client = get_upstox_client()
        client.ensure_session()
        
        url = "https://api.upstox.com/v2/portfolio/short-term-positions"
        r = client._http.get(url, headers=client._headers(), timeout=10)
        
        realized = 0.0
        unrealized = 0.0
        positions = []
        trades = 0
        wins = 0
        losses = 0
        
        if r.ok:
            data = r.json()
            if data.get("status") == "success" and data.get("data"):
                for pos in data["data"]:
                    rpnl = float(pos.get("realised", 0) or pos.get("realised_profit", 0) or 0)
                    upnl = float(pos.get("unrealised", 0) or pos.get("unrealised_profit", 0) or 0)
                    realized += rpnl
                    unrealized += upnl
                    
                    qty = int(pos.get("quantity", 0) or 0)
                    if qty != 0:
                        positions.append({
                            "symbol": pos.get("tradingsymbol", ""),
                            "direction": "LONG" if qty > 0 else "SHORT",
                            "entry_price": float(pos.get("average_price", 0) or 0),
                            "current_price": float(pos.get("last_price", 0) or 0),
                            "pnl_inr": upnl,
                            "quantity": abs(qty)
                        })
                    
                    if rpnl != 0:
                        trades += 1
                        if rpnl > 0:
                            wins += 1
                        else:
                            losses += 1
                            
        return {
            "total_realized_pnl": realized,
            "total_unrealized_pnl": unrealized,
            "net_pnl": realized + unrealized,
            "positions": positions,
            "trade_count": trades,
            "win_count": wins,
            "loss_count": losses
        }
    except Exception as exc:
        logger.error(f"Daybook error: {exc}")
        return JSONResponse(status_code=500, content={"detail": str(exc)})

@app.get("/api/orders/book", dependencies=[Depends(_check_auth)])
@limiter.limit("30/minute")
def get_orders_book(request: Request, limit: int = Query(100)):
    try:
        from oms import get_oms
        oms = get_oms()
        intents = oms.intents(limit)
    except:
        intents = []
        
    try:
        from order_events import get_store
        fills = get_store().recent(limit)
    except:
        fills = []
        
    return {
        "intents": intents,
        "fills": fills
    }

@app.get("/api/options/expiry")
@limiter.limit("30/minute")
def get_options_expiry(request: Request):
    try:
        from broker_upstox import get_upstox_client, _parse_expiry
        from datetime import datetime
        client = get_upstox_client()
        _, fo_index = client.instrument_map()
        
        now_date = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        expiries = set()
        
        for underlying, contracts in fo_index.items():
            for c in contracts:
                exp = _parse_expiry(c.get("expiry"))
                if exp and exp >= now_date:
                    expiries.add(exp.strftime("%Y-%m-%d"))
                    
        sorted_expiries = sorted(list(expiries))
        return {"expiries": sorted_expiries[:30]}
    except Exception as exc:
        logger.error(f"Expiry fetch error: {exc}")
        return JSONResponse(status_code=500, content={"detail": str(exc)})


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
            # The client sends JSON.stringify({type:"ping"}), so the bare
            # string comparison never matched and no pong was ever returned —
            # the heartbeat was dead in both directions and neither side could
            # detect a half-open connection. Accept both shapes.
            command = data.strip()
            if command.startswith("{"):
                try:
                    command = str((json.loads(data) or {}).get("type", ""))
                except (ValueError, TypeError):
                    command = ""
            if command == "ping":
                await websocket.send_json({"type": "pong"})
            elif command == "stats":
                await websocket.send_json({"type": "stats", "stats": engine.get_stats()})
    except WebSocketDisconnect:
        _ws_clients.discard(websocket)
    except Exception:
        _ws_clients.discard(websocket)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    # Loopback by default. This server exposes settings writes and an order
    # endpoint, and APEX_API_KEY is unset in every checked-in configuration, so
    # _check_auth returns immediately — binding 0.0.0.0 published an
    # unauthenticated control plane to the whole LAN. Set APEX_BIND explicitly
    # to widen it, and set APEX_API_KEY when you do.
    host = os.environ.get("APEX_BIND", "127.0.0.1").strip() or "127.0.0.1"
    if host != "127.0.0.1" and not os.getenv("APEX_API_KEY", "").strip():
        logger.warning(
            "APEX_BIND=%s exposes this server beyond loopback while APEX_API_KEY is unset; "
            "settings and order endpoints will be unauthenticated.", host,
        )
    # The desktop launcher owns process restarts.  Uvicorn's reload supervisor
    # creates a parent/child pair on Windows, which left a supervisor alive after
    # the launcher killed the port-owning child.  The supervisor then respawned
    # the child and caused the next launch to fail with WinError 10013.
    uvicorn.run(app, host=host, port=port, log_level="info")
