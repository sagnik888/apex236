import json
from fastapi import WebSocket

LATEST_SCANS: dict[str, dict] = {}
# Full-universe compact signal snapshots used by the multi-timeframe dashboard.
# Kept separate from LATEST_SCANS so WebSocket quote pulses stay lightweight.
LATEST_SYMBOL_STATES: dict[str, dict[str, dict]] = {}
SCHEDULER_STATUS: dict[str, dict] = {}
_ws_clients: set[WebSocket] = set()


async def broadcast_update(timeframe: str, payload: dict):
    LATEST_SCANS[timeframe] = payload
    dead = set()
    message = json.dumps({"type": "update", "timeframe": timeframe, "data": payload})
    for ws in list(_ws_clients):
        try:
            await ws.send_text(message)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)
