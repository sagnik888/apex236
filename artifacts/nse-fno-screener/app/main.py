from __future__ import annotations
import asyncio, json
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.engine.timeframes import TF_GROUPS
from app.main_state import LATEST_SCANS, LATEST_SYMBOL_STATES, SCHEDULER_STATUS, _ws_clients
from app.market_calendar import market_status

VALID_TFS = set(TF_GROUPS["intraday"] + TF_GROUPS["swing"] + TF_GROUPS["positional"])

@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.scheduler import run_scheduler
    task = asyncio.create_task(run_scheduler(), name="scan-scheduler")
    app.state.scheduler_task = task
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

app = FastAPI(title="NSE F&O Multi-Timeframe Screener", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")

@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")

@app.get("/api/health")
async def health():
    return {"status": "ok", "market_status": market_status(), "as_of": datetime.now(timezone.utc).isoformat(),
            "computed_timeframes": sorted(LATEST_SCANS.keys()), "scheduler": SCHEDULER_STATUS}

@app.get("/api/scans")
async def all_scans():
    return LATEST_SCANS


@app.get("/api/intraday-matrix")
async def intraday_matrix():
    """Compact full-universe signal state for the five intraday columns.

    This endpoint is local-memory only: calling it does NOT ping Yahoo or
    Upstox. It is refreshed by the existing micro/medium scan lanes.
    """
    ordered = ["5min", "15min", "30min", "1h", "4h"]
    return {
        "timeframes": ordered,
        "states": {tf: LATEST_SYMBOL_STATES.get(tf, {}) for tf in ordered},
        "generated_at": {tf: (LATEST_SCANS.get(tf) or {}).get("generated_at") for tf in ordered},
    }

@app.get("/api/scan/{timeframe}")
async def get_scan(timeframe: str):
    if timeframe not in VALID_TFS:
        raise HTTPException(400, f"Unsupported timeframe. Use: {sorted(VALID_TFS)}")
    return LATEST_SCANS.get(timeframe, {"status": "not_yet_computed", "timeframe": timeframe})

@app.post("/api/refresh/{timeframe}")
async def refresh(timeframe: str):
    if timeframe not in VALID_TFS:
        raise HTTPException(400, f"Unsupported timeframe. Use: {sorted(VALID_TFS)}")
    from app.scheduler import refresh_timeframes
    asyncio.create_task(refresh_timeframes([timeframe], force=True))
    return {"status": "refresh_started", "timeframe": timeframe}

@app.websocket("/ws/scan")
async def ws_scan(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        await websocket.send_text(json.dumps({"type": "snapshot", "data": LATEST_SCANS}))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(websocket)
