"""Bulk Upstox quote cache.

One request can cover the complete ~237-symbol F&O universe, so the fast path
never fans out into one REST call per stock.  A small TTL + request-gap guard
also means concurrent timeframe scans reuse the same snapshot.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from app import config

URL = "https://api.upstox.com/v2/market-quote/quotes?instrument_key="
IST = ZoneInfo("Asia/Kolkata")

_cache: dict[str, dict] = {}
_cache_mono = 0.0
_lock = asyncio.Lock()
_request_times: deque[float] = deque()
_last_error: str | None = None
_last_success_at: str | None = None


def _normalise_trade_time(value) -> str | None:
    if value in (None, ""):
        return None
    try:
        # Full Market Quote uses epoch milliseconds for last_trade_time.
        if isinstance(value, (int, float)) or str(value).isdigit():
            n = int(value)
            if n > 10_000_000_000:
                n = n / 1000
            return datetime.fromtimestamp(n, tz=timezone.utc).astimezone(IST).isoformat()
        return str(value)
    except Exception:
        return str(value)


def _fetch(keys: list[str]) -> dict[str, dict]:
    if not keys:
        return {}
    # Official endpoint limit is 500 keys/request; this project has ~237.
    encoded = ",".join(quote(k, safe="|") for k in keys[:500])
    req = Request(f"http://127.0.0.1:8080/api/quotes/bulk?keys={encoded}", headers={
        "Accept": "application/json",
        "User-Agent": "NSE-FNO-Screener/4.0",
    })
    with urlopen(req, timeout=12) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    out: dict[str, dict] = {}
    for response_key, item in (payload.get("data") or {}).items():
        token = item.get("instrument_token") or response_key
        if not token:
            continue
        item = dict(item)
        item["last_trade_time_iso"] = _normalise_trade_time(item.get("last_trade_time"))
        out[token] = item
    return out


def _trim_request_window(now: float) -> None:
    cutoff = now - 1800.0
    while _request_times and _request_times[0] < cutoff:
        _request_times.popleft()


async def refresh_live_quotes(keys: list[str], force: bool = False) -> dict[str, dict]:
    """Refresh one bulk snapshot, safely shared by every timeframe.

    Even with force=True we enforce a 5-second hard gap.  At the default 15s
    pulse this is only 4 REST requests/minute (120/30m), leaving a very large
    margin below Upstox standard API limits.
    """
    global _cache, _cache_mono, _last_error, _last_success_at
    if not keys:
        return {}

    async with _lock:
        now = time.monotonic()
        age = now - _cache_mono if _cache_mono else float("inf")
        if _cache and not force and age < config.QUOTE_REFRESH_SECONDS:
            return _cache
        # Never allow accidental UI/manual concurrency to create a burst.
        if _cache and age < 5.0:
            return _cache
        try:
            fresh = await asyncio.to_thread(_fetch, keys)
            if fresh:
                _cache = fresh
                _cache_mono = time.monotonic()
                _last_error = None
                _last_success_at = datetime.now(IST).isoformat()
                _request_times.append(_cache_mono)
                _trim_request_window(_cache_mono)
        except Exception as exc:
            _last_error = str(exc)
        return _cache


async def get_live_quotes(keys: list[str], force: bool = False) -> dict[str, dict]:
    cache = await refresh_live_quotes(keys, force=force)
    if not keys:
        return {}
    wanted = set(keys)
    return {k: v for k, v in cache.items() if k in wanted}


def quote_stats() -> dict:
    now = time.monotonic()
    _trim_request_window(now)
    age = round(now - _cache_mono, 1) if _cache_mono else None
    return {
        "enabled": True,
        "symbols_cached": len(_cache),
        "cache_age_seconds": age,
        "last_success_at": _last_success_at,
        "last_error": _last_error,
        "rest_requests_last_30m": len(_request_times),
        "target_refresh_seconds": config.QUOTE_REFRESH_SECONDS,
    }
