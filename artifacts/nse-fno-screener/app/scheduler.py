from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from app import config
from app.engine.pipeline import compute_scan_for_timeframe
from app.instrument_master import sync_instruments
from app.live_quotes import get_live_quotes, quote_stats, refresh_live_quotes
from app.main_state import LATEST_SCANS, LATEST_SYMBOL_STATES, SCHEDULER_STATUS, broadcast_update
from app.market_calendar import IST, market_status

log = logging.getLogger("scheduler")

MICRO_TFS = ["5min", "15min", "30min"]
MEDIUM_TFS = ["1h", "4h"]
MACRO_TFS = ["1d", "2d", "4d", "1w"]

_tf_locks: dict[str, asyncio.Lock] = {}
_last_manual_force: dict[str, float] = {}


def _is_live() -> bool:
    return market_status() == "LIVE"


def _set_loop_status(name: str, **values) -> None:
    loop = SCHEDULER_STATUS.setdefault("loops", {}).setdefault(name, {})
    loop.update(values)


async def _refresh_one(tf: str, force: bool = False) -> dict:
    lock = _tf_locks.setdefault(tf, asyncio.Lock())
    if lock.locked() and not force:
        return LATEST_SCANS.get(tf, {"status": "refresh_already_running", "timeframe": tf})
    async with lock:
        try:
            payload = await compute_scan_for_timeframe(tf, force=force)
            symbol_states = payload.pop("_symbol_states", None)
            if symbol_states is not None:
                LATEST_SYMBOL_STATES[tf] = symbol_states
            await broadcast_update(tf, payload)
            return payload
        except Exception as exc:
            log.exception("Failed to refresh %s", tf)
            payload = {"status": "error", "timeframe": tf, "error": str(exc), "generated_at": datetime.now(IST).isoformat()}
            await broadcast_update(tf, payload)
            return payload


async def refresh_timeframes(timeframes: list[str], force: bool = False):
    """Refresh timeframes concurrently; no global lock blocks micro vs macro."""
    if force:
        now = time.monotonic()
        allowed: list[str] = []
        for tf in timeframes:
            last = _last_manual_force.get(tf, 0.0)
            if now - last >= config.MANUAL_REFRESH_COOLDOWN_SECONDS:
                _last_manual_force[tf] = now
                allowed.append(tf)
        timeframes = allowed
    if not timeframes:
        return []
    return await asyncio.gather(*(_refresh_one(tf, force=force) for tf in timeframes))


async def _scan_loop(name: str, timeframes: list[str], live_seconds: int, closed_seconds: int, initial_delay: int = 0):
    if initial_delay:
        await asyncio.sleep(initial_delay)
    while True:
        started = datetime.now(IST)
        _set_loop_status(name, state="running", started_at=started.isoformat(), timeframes=timeframes)
        results = await refresh_timeframes(timeframes)
        ended = datetime.now(IST)
        cadence = live_seconds if _is_live() else closed_seconds
        elapsed = (ended - started).total_seconds()
        sleep_for = max(1.0, cadence - elapsed)
        _set_loop_status(
            name,
            state="sleeping",
            last_completed_at=ended.isoformat(),
            last_duration_seconds=round(elapsed, 2),
            target_start_to_start_seconds=cadence,
            next_in_seconds=round(sleep_for, 1),
            successful=sum(1 for r in results if r and r.get("status") == "ok"),
        )
        await asyncio.sleep(sleep_for)


def _overlay_quote_rows(payload: dict, symbol_to_key: dict[str, str], quotes: dict[str, dict]) -> bool:
    changed = False
    for side in ("bullish", "bearish"):
        for row in payload.get(side, []) or []:
            key = symbol_to_key.get(row.get("symbol"))
            q = quotes.get(key) if key else None
            if not q:
                continue
            price = q.get("last_price")
            if price is not None:
                new_price = round(float(price), 2)
                if row.get("last_price") != new_price:
                    row["last_price"] = new_price
                    changed = True
            row["last_quote_time"] = q.get("last_trade_time_iso") or q.get("last_trade_time")
            row["price_is_live"] = True
            row["data_source"] = "upstox_quote+cached_closed_bars"
    if quotes:
        payload["quote_refreshed_at"] = datetime.now(IST).isoformat()
        payload["quote_cache"] = quote_stats()
    return changed


async def quote_loop():
    """Independent price pulse. Never waits for indicator scans."""
    await asyncio.sleep(3)  # let the first micro scan start immediately
    while True:
        started = datetime.now(IST)
        try:
            instruments, _ = await asyncio.to_thread(sync_instruments, False)
            symbol_to_key = {r["symbol"]: r.get("upstox_instrument_key") for r in instruments if r.get("upstox_instrument_key")}
            keys = list(symbol_to_key.values())
            quotes = await refresh_live_quotes(keys, force=True)
            # Broadcast quote-overlaid payloads. Scores stay closed-bar stable;
            # only the live price/timestamp pulses between scan recomputations.
            if quotes:
                for tf, payload in list(LATEST_SCANS.items()):
                    if payload.get("status") != "ok":
                        continue
                    _overlay_quote_rows(payload, symbol_to_key, quotes)
                    await broadcast_update(tf, payload)
            _set_loop_status("quotes", state="sleeping", last_completed_at=datetime.now(IST).isoformat(), quote_stats=quote_stats())
        except Exception as exc:
            log.exception("Quote pulse failed")
            _set_loop_status("quotes", state="error", error=str(exc), last_error_at=datetime.now(IST).isoformat())
        cadence = config.QUOTE_REFRESH_SECONDS if _is_live() else config.CLOSED_QUOTE_REFRESH_SECONDS
        elapsed = (datetime.now(IST) - started).total_seconds()
        sleep_for = max(1.0, cadence - elapsed)
        _set_loop_status("quotes", next_in_seconds=round(sleep_for,1), target_start_to_start_seconds=cadence, last_duration_seconds=round(elapsed,2))
        await asyncio.sleep(sleep_for)


async def run_scheduler():
    SCHEDULER_STATUS.update({
        "architecture": "two_speed_nonblocking",
        "micro_timeframes": MICRO_TFS,
        "medium_timeframes": MEDIUM_TFS,
        "macro_timeframes": MACRO_TFS,
        "quote_refresh_seconds": config.QUOTE_REFRESH_SECONDS,
        "micro_scan_seconds": config.MICRO_SCAN_SECONDS,
        "medium_scan_seconds": config.MEDIUM_SCAN_SECONDS,
        "macro_scan_seconds": config.MACRO_SCAN_SECONDS,
        "started_at": datetime.now(IST).isoformat(),
    })
    await asyncio.gather(
        _scan_loop("micro", MICRO_TFS, config.MICRO_SCAN_SECONDS, config.CLOSED_MICRO_SCAN_SECONDS, 0),
        _scan_loop("medium", MEDIUM_TFS, config.MEDIUM_SCAN_SECONDS, config.CLOSED_MEDIUM_SCAN_SECONDS, 8),
        _scan_loop("macro", MACRO_TFS, config.MACRO_SCAN_SECONDS, config.CLOSED_MACRO_SCAN_SECONDS, 18),
        quote_loop(),
    )
