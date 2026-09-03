"""Market data provider: AngelOne SmartAPI (live) primary, Yahoo Finance fallback.

Architecture
------------
* 15m / 1h / 4h (intraday): AngelOne
    - REST ``getCandleData`` bootstraps per-symbol history once per day
      (pickled to ``angel_cache/`` so restarts are cheap)
    - the WebSocket tick feed (``angel_feed``) supplies the live forming candle
      and locally-built completed candles between REST refreshes
    - a background healer re-fetches official candles to replace tick-built
      ones and to repair gaps (WS downtime), rate-limit friendly
    - 1h is resampled from Angel 1h REST history extended by live 15m data;
      4h is resampled from 1h with NSE 09:15 session anchoring
* 1d (large timeframe): Yahoo Finance (2y, auto-adjusted) as before
* Fallback: any Angel failure (credentials, login, per-symbol data) falls back
  to the original Yahoo batch path automatically.

Fixes rolled into this rewrite (from the audit):
  - cache entries are no longer re-stamped on read (freshness laundering)
  - the single-symbol bulk fallback only caches when exactly one symbol
    was requested (previously it could attribute data to the wrong symbol)
  - the per-ticker "LTP injection" is gone; live prices now come from one
    consistent source (the tick feed) for every consumer
"""
from __future__ import annotations

import logging
import multiprocessing
import os
import threading
import time
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

logger = logging.getLogger(__name__)

IST = "Asia/Kolkata"
_IST_TZ = ZoneInfo(IST)
HERE = Path(__file__).resolve().parent
ANGEL_CACHE_DIR = HERE / "angel_cache"

# ── Yahoo fetch config (fallback + 1d) ───────────────────────────────────────
_FETCH_CONFIG: dict[str, dict] = {
    "5m":  {"interval": "5m",  "period": "60d"},
    "15m": {"interval": "15m", "period": "60d"},
    "30m": {"interval": "30m", "period": "60d"},
    "1h":  {"interval": "1h",  "period": "2y"},
    "4h":  {"interval": "1h",  "period": "2y"},   # resample 1h -> 4h
    "1d":  {"interval": "1d",  "period": "2y"},
}

# NSE-aligned session origin — intraday buckets start at 09:15 IST every day
_SESSION_ORIGIN = pd.Timestamp("2000-01-03 09:15:00", tz=IST)

_BATCH_CACHE: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
_BATCH_CACHE_LIMIT = 2000
_CHAIN_CACHE: dict[tuple[str, Optional[str]], tuple[float, list[dict]]] = {}

def _enforce_cache_limit() -> None:
    if len(_BATCH_CACHE) > _BATCH_CACHE_LIMIT:
        # Remove the oldest 10% of entries based on timestamp
        sorted_keys = sorted(_BATCH_CACHE.keys(), key=lambda k: _BATCH_CACHE[k][0])
        for k in sorted_keys[:int(_BATCH_CACHE_LIMIT * 0.1)]:
            _BATCH_CACHE.pop(k, None)

_NSE_OPEN = dt_time(9, 15)
_NSE_CLOSE = dt_time(15, 30)
_LIVE_CACHE_TTL = 45
_OFF_MARKET_CACHE_TTL = 300

# ── Angel state ───────────────────────────────────────────────────────────────
_ANGEL_MODE = os.getenv("APEX_DATA_SOURCE", "auto").lower()   # auto | angel | yahoo
_ANGEL_DISABLED_UNTIL = 0.0          # monotonic; cooldown after hard failures
_ANGEL_LOCK = threading.RLock()
_ANGEL_TOKENS: dict[str, dict] = {}  # "RELIANCE.NS" -> {token, tradingsymbol}
_ANGEL_15M: dict[str, pd.DataFrame] = {}   # completed 15m candles per symbol
_ANGEL_1H: dict[str, pd.DataFrame] = {}    # completed 1h candles per symbol
_BOOTSTRAP_STATE = {"15m": "idle", "1h": "idle"}  # idle|running|ready|failed
_BOOTSTRAP_EVENTS = {"15m": threading.Event(), "1h": threading.Event()}
_HEAL_QUEUE: set[str] = set()
_HEAL_LOCK = threading.Lock()
_HEAL_THREAD: Optional[threading.Thread] = None
_LAST_SCAN_SOURCE: dict[tuple[str, str], str] = {}   # (symbol, timeframe) -> "angel" | "yahoo"

_OHLCV_COLS = ["open", "high", "low", "close", "volume"]


def _is_main_process() -> bool:
    return multiprocessing.current_process().name == "MainProcess"


def _now_ist() -> datetime:
    return datetime.now(_IST_TZ)


def _cache_ttl_seconds(now: Optional[datetime] = None) -> int:
    now = now or _now_ist()
    if now.tzinfo is None:
        now = now.replace(tzinfo=_IST_TZ)
    else:
        now = now.astimezone(_IST_TZ)
    local_time = now.time().replace(tzinfo=None)
    try:
        from market_calendar import is_trading_day
        trading_day = is_trading_day(now.date())
    except Exception:
        trading_day = now.weekday() < 5
    # FIX DATA-02: Add a 5-minute post-close buffer before relaxing the TTL.
    # Otherwise, a 15:29:15 cache fetch is seen as "fresh" at 15:30:01 because
    # the TTL jumps from 45s to 300s, leading to EOD scans on incomplete data.
    from datetime import time
    _nse_close_buffer = time(15, 35)
    return _LIVE_CACHE_TTL if trading_day and _NSE_OPEN <= local_time <= _nse_close_buffer else _OFF_MARKET_CACHE_TTL


# ═══════════════════════════════════════════════════════════════════════════
# Angel primary path
# ═══════════════════════════════════════════════════════════════════════════

def _angel_enabled() -> bool:
    if _ANGEL_MODE == "yahoo" or not _is_main_process():
        return False
    if time.monotonic() < _ANGEL_DISABLED_UNTIL:
        return False
    try:
        from broker_angel import credentials_available
        return credentials_available()
    except Exception:
        return False


def _disable_angel(reason: str, cooldown: float = 300.0) -> None:
    global _ANGEL_DISABLED_UNTIL
    _ANGEL_DISABLED_UNTIL = time.monotonic() + cooldown
    logger.error("Angel data source disabled for %.0fs: %s", cooldown, reason)


def _angel_client():
    from broker_angel import get_client
    return get_client()


def _ensure_tokens(symbols: list[str]) -> bool:
    global _ANGEL_TOKENS
    with _ANGEL_LOCK:
        missing = [s for s in symbols if s not in _ANGEL_TOKENS]
        if not missing:
            return True
        try:
            resolved, unresolved = _angel_client().resolve(missing)
            _ANGEL_TOKENS.update(resolved)
            if unresolved:
                logger.warning("Angel: %s unresolved symbols will use Yahoo: %s", len(unresolved), unresolved)
            return True
        except Exception as exc:
            _disable_angel(f"token resolution failed: {exc}")
            return False


def _pickle_path(symbol: str, interval: str) -> Path:
    safe = symbol.replace(".NS", "").replace("&", "_AND_").replace("-", "_")
    return ANGEL_CACHE_DIR / f"{safe}__{interval}.pkl"


def _load_pickle_if_fresh(symbol: str, interval: str) -> Optional[pd.DataFrame]:
    path = _pickle_path(symbol, interval)
    try:
        if path.exists():
            frame = pd.read_pickle(path)
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                # FIX: Check actual data freshness, not just file modification time
                # A file modified yesterday could contain data from weeks ago
                last_bar = pd.Timestamp(frame.index[-1])
                if last_bar.tzinfo is None:
                    last_bar = last_bar.tz_localize(_IST_TZ)
                else:
                    last_bar = last_bar.astimezone(_IST_TZ)
                
                now = _now_ist()
                age_hours = (now - last_bar).total_seconds() / 3600
                
                # For intraday (15m/1h): reject if data is older than 24 hours
                # For daily: reject if data is older than 3 days
                max_age_hours = 24 if interval in ("15m", "1h") else 72
                
                if age_hours <= max_age_hours:
                    return frame
                else:
                    logger.debug(
                        "Pickle for %s/%s rejected: data is %.1f hours old (last bar: %s)",
                        symbol, interval, age_hours, last_bar
                    )
    except Exception:
        pass
    return None


def _save_pickle(symbol: str, interval: str, frame: pd.DataFrame) -> None:
    try:
        ANGEL_CACHE_DIR.mkdir(exist_ok=True)
        frame.to_pickle(_pickle_path(symbol, interval))
    except Exception:
        pass


def _last_completed_15m_open(now: Optional[datetime] = None) -> Optional[pd.Timestamp]:
    """Bar-open time of the most recently COMPLETED 15m candle."""
    now = now or _now_ist()
    day_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if now < day_open + timedelta(minutes=15):
        return None  # today's first candle not complete yet (may be prior day; fine)
    minutes = int((now - day_open).total_seconds() // 60)
    completed_buckets = min(minutes // 15, 25)  # 25 buckets: 09:15..15:15
    return pd.Timestamp(day_open + timedelta(minutes=(completed_buckets - 1) * 15))


def drop_non_session_bars(frame: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """Remove bars dated on a non-trading day from an OHLCV frame.

    Brokers happily return candles for exchange MOCK / disaster-recovery
    sessions (NSE ran them on Saturdays 2026-07-25 and 2026-08-01). Those bars
    are a systems test, not a market: the 2026-08-01 mock printed RELIANCE
    between 1270 and 1569 against a real 2026-07-31 close of 1305. Merged into
    the store they inflated ATR(14) on the 1h series by a median 4.47x across
    234 of 236 symbols, which pushed the raw ATR stop from ~1.28% to ~5.93% of
    price and pinned every subsequent intraday trade against the stop clamp.

    Applied to the MERGED result rather than only to new data, so an existing
    contaminated store is cleaned on the next merge instead of persisting.
    """
    if frame is None or frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
        return frame
    try:
        from market_calendar import is_ingestable
    except Exception:  # calendar unavailable — do not silently drop real data
        return frame
    dates = pd.Series(frame.index.date, index=frame.index)
    keep = dates.map(is_ingestable).to_numpy(dtype=bool)
    if keep.all():
        return frame
    dropped = sorted({d for d, k in zip(dates.to_numpy(), keep) if not k})
    logger.warning(
        "Dropped %d non-session bar(s) from %s", int((~keep).sum()),
        ", ".join(str(d) for d in dropped[:5]),
    )
    return frame[keep]


def _merge_frames(base: Optional[pd.DataFrame], extra: pd.DataFrame) -> pd.DataFrame:
    if base is None or base.empty:
        merged = extra
    else:
        merged = pd.concat([base, extra])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    return drop_non_session_bars(merged)


def _drop_forming_bucket(frame: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Remove the still-forming trailing candle from a REST result.

    Angel's getCandleData includes the in-progress candle; the store must hold
    COMPLETED candles only (the live forming candle comes from the tick feed).
    """
    if frame is None or frame.empty:
        return frame
    now = _now_ist()
    last_open_ts = pd.Timestamp(frame.index[-1])
    if last_open_ts.tzinfo is None:
        last_open_ts = last_open_ts.tz_localize(_IST_TZ)
    if last_open_ts + pd.Timedelta(minutes=minutes) > pd.Timestamp(now):
        return frame.iloc[:-1]
    return frame


def _bootstrap_interval(symbols: list[str], interval: str, lookback_days: int) -> None:
    """Fetch per-symbol history from Angel REST; runs on a background thread."""
    store = _ANGEL_15M if interval == "15m" else _ANGEL_1H
    state_key = interval
    _BOOTSTRAP_STATE[state_key] = "running"
    client = _angel_client()
    now = _now_ist()
    
    _done_lock = threading.Lock()
    _failed_lock = threading.Lock()
    done = 0
    failed = 0
    started = time.monotonic()

    def _process_symbol(symbol: str) -> None:
        nonlocal done, failed
        if symbol not in _ANGEL_TOKENS:
            return
        try:
            with _ANGEL_LOCK:
                existing = store.get(symbol)
            if existing is None:
                existing = _load_pickle_if_fresh(symbol, interval)
                if existing is not None:
                    with _ANGEL_LOCK:
                        store[symbol] = existing
            fetch_from = now - timedelta(days=lookback_days)
            if existing is not None and len(existing) > 50:
                fetch_from = existing.index[-1].to_pydatetime() - timedelta(days=2)
            frame = client.get_candles(_ANGEL_TOKENS[symbol]["token"], interval, fetch_from, now)
            if frame is None:
                with _failed_lock:
                    failed += 1
                return
            frame = _drop_forming_bucket(frame, 15 if interval == "15m" else 60)
            if not frame.empty:
                merged = _merge_frames(existing, frame)
                with _ANGEL_LOCK:
                    store[symbol] = merged
                _save_pickle(symbol, interval, merged)
            with _done_lock:
                done += 1
                if done % 40 == 0:
                    logger.info("Angel %s bootstrap: %s/%s symbols (%.0fs elapsed)",
                                interval, done, len(symbols), time.monotonic() - started)
        except Exception as exc:
            with _failed_lock:
                failed += 1
            logger.warning("Angel bootstrap %s/%s failed: %s", symbol, interval, exc)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as executor:
        executor.map(_process_symbol, symbols)

    _BOOTSTRAP_STATE[state_key] = "ready" if done > 0 else "failed"
    _BOOTSTRAP_EVENTS[state_key].set()
    logger.info("Angel %s bootstrap finished: ok=%s failed=%s in %.0fs",
                interval, done, failed, time.monotonic() - started)


def _start_bootstrap(symbols: list[str], interval: str, lookback_days: int) -> None:
    if _BOOTSTRAP_STATE[interval] in ("running", "ready"):
        return
    _BOOTSTRAP_STATE[interval] = "running"
    threading.Thread(
        target=_bootstrap_interval,
        args=(list(symbols), interval, lookback_days),
        name=f"angel-bootstrap-{interval}",
        daemon=True,
    ).start()


def _absorb_feed_candles(symbols: list[str]) -> None:
    """Move tick-built completed candles from the feed into the 15m store."""
    from angel_feed import get_feed
    feed = get_feed()
    if not feed.is_connected() and feed.seconds_since_last_packet() is None:
        return
    healed = 0
    for symbol in symbols:
        row = _ANGEL_TOKENS.get(symbol)
        if not row:
            continue
        with _ANGEL_LOCK:
            base = _ANGEL_15M.get(symbol)
        last_ts = base.index[-1] if base is not None and len(base) else None
        candles = feed.get_completed_candles(row["token"], since=last_ts)
        if not candles:
            continue
        frame = pd.DataFrame(
            [{"open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"], "volume": c["volume"]}
             for c in candles],
            index=pd.DatetimeIndex([pd.Timestamp(c["start"]) for c in candles]),
        )
        with _ANGEL_LOCK:
            _ANGEL_15M[symbol] = _merge_frames(base, frame)
        # Tick-built candles are approximations — queue official re-fetch.
        with _HEAL_LOCK:
            _HEAL_QUEUE.add(symbol)
        healed += 1
    if healed:
        _ensure_heal_thread()


def _detect_gaps(symbols: list[str]) -> None:
    """Queue symbols whose stored history lags the last completed candle."""
    expected = _last_completed_15m_open()
    if expected is None:
        return
    for symbol in symbols:
        with _ANGEL_LOCK:
            base = _ANGEL_15M.get(symbol)
        if base is None or not len(base):
            continue
        if base.index[-1] < expected - pd.Timedelta(minutes=15):
            with _HEAL_LOCK:
                _HEAL_QUEUE.add(symbol)
    with _HEAL_LOCK:
        has_heal = bool(_HEAL_QUEUE)
    if has_heal:
        _ensure_heal_thread()


def _ensure_heal_thread() -> None:
    global _HEAL_THREAD
    if _HEAL_THREAD is not None and _HEAL_THREAD.is_alive():
        return

    def _heal_loop() -> None:
        client = _angel_client()
        while True:
            try:
                with _HEAL_LOCK:
                    symbol = _HEAL_QUEUE.pop()
            except KeyError:
                time.sleep(5)
                continue
            row = _ANGEL_TOKENS.get(symbol)
            if not row:
                continue
            try:
                with _ANGEL_LOCK:
                    base = _ANGEL_15M.get(symbol)
                frm = (_now_ist() - timedelta(days=5)) if base is None or not len(base) else \
                    base.index[-1].to_pydatetime() - timedelta(hours=6)
                frame = client.get_candles(row["token"], "15m", frm, _now_ist())
                frame = _drop_forming_bucket(frame, 15) if frame is not None else None
                if frame is not None and not frame.empty:
                    with _ANGEL_LOCK:
                        _ANGEL_15M[symbol] = _merge_frames(base, frame)
            except Exception as exc:
                logger.debug("heal %s failed: %s", symbol, exc)

    _HEAL_THREAD = threading.Thread(target=_heal_loop, name="angel-heal", daemon=True)
    _HEAL_THREAD.start()


def _forming_candle_row(symbol: str) -> Optional[pd.DataFrame]:
    """Current forming 15m candle from the tick feed as a one-row frame."""
    row = _ANGEL_TOKENS.get(symbol)
    if not row:
        return None
    from angel_feed import get_feed
    candle = get_feed().get_forming_candle(row["token"])
    if not candle:
        return None
    return pd.DataFrame(
        [{"open": candle["open"], "high": candle["high"], "low": candle["low"],
          "close": candle["close"], "volume": candle["volume"]}],
        index=pd.DatetimeIndex([pd.Timestamp(candle["start"])]),
    )


def _angel_15m_frame(symbol: str) -> Optional[pd.DataFrame]:
    with _ANGEL_LOCK:
        base = _ANGEL_15M.get(symbol)
        frame = base.copy() if base is not None else None
    if frame is None or len(frame) < 50:
        return None
    forming = _forming_candle_row(symbol)
    if forming is not None:
        forming_start = forming.index[-1]
        # FIX DATA-01: Stale Tick Overwrites Official Close
        # If the forming candle overlaps with an already fetched historical bar,
        # it means the historical fetch already contains the definitive close for that bucket.
        # Do not overwrite the official exchange close with a stale websocket LTP.
        if forming_start > frame.index[-1]:
            frame = pd.concat([frame, forming])
    return frame.tail(700)


def _resample_intraday(frame: pd.DataFrame, rule: str, symbol: Optional[str] = None) -> pd.DataFrame:
    rs = frame.resample(rule, origin=_SESSION_ORIGIN, closed="left", label="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["open", "high", "low", "close"])
    
    if symbol is not None and not symbol.endswith(".NS"):
        return rs
    
    return rs[rs.index.hour.isin(range(9, 16))]


def _angel_1h_frame(symbol: str, tail: int = 700) -> Optional[pd.DataFrame]:
    with _ANGEL_LOCK:
        base = _ANGEL_1H.get(symbol)
        base = base.copy() if base is not None else None
    m15 = _angel_15m_frame(symbol)
    live_1h = _resample_intraday(m15, "60min", symbol) if m15 is not None else None
    if base is not None and len(base) > 100:
        if live_1h is not None and len(live_1h):
            cutoff = base.index[-1] - pd.Timedelta(days=1)
            merged = _merge_frames(base, live_1h[live_1h.index > cutoff])
        else:
            merged = base
        return merged.tail(tail)
    # 1h bootstrap not ready — a 15m-derived series still covers max_input_bars
    return live_1h.tail(tail) if live_1h is not None and len(live_1h) > 100 else None


def _angel_fetch(symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
    if timeframe == "15m":
        return _angel_15m_frame(symbol)
    if timeframe == "1h":
        return _angel_1h_frame(symbol)
    if timeframe == "4h":
        # The engine needs >=200 completed 4h candles (min_history_bars), i.e.
        # ~1300 hourly bars. A 700-bar tail here starved the 4h scan into
        # permanent FLAT across the whole universe.
        hourly = _angel_1h_frame(symbol, tail=2000)
        if hourly is None or len(hourly) < 100:
            return None
        return _resample_intraday(hourly, "4h", symbol).tail(450)
    return None


def get_data_health() -> dict:
    """Freshness/completeness snapshot for health reporting."""
    health: dict[str, object] = {
        "source_mode": _ANGEL_MODE,
        "angel_active": _angel_enabled(),
        "bootstrap": dict(_BOOTSTRAP_STATE),
        "heal_queue": len(_HEAL_QUEUE),
        "symbols_cached_15m": len(_ANGEL_15M),
        # Delayed-fallback count for INTRADAY timeframes only; 1d uses Yahoo
        # by design and must not trip the health check.
        "source_by_symbol_yahoo": len({
            symbol for (symbol, timeframe), source in _LAST_SCAN_SOURCE.items()
            if source == "yahoo" and timeframe in ("15m", "1h", "4h")
        }),
    }
    try:
        from angel_feed import get_feed
        health["feed"] = get_feed().stats()
    except Exception:
        health["feed"] = None
    try:
        from broker_dispatcher import get_dispatcher
        health["dispatcher"] = get_dispatcher().status()
    except Exception:
        health["dispatcher"] = None
    return health


# ═══════════════════════════════════════════════════════════════════════════
# Public API (used by scanner_engine)
# ═══════════════════════════════════════════════════════════════════════════

def prefetch_all_ohlcv(symbols: list[str], timeframes: list[str]) -> None:
    """Prepare data for a scan pass over ``symbols`` × ``timeframes``."""
    timeframes = [tf for tf in dict.fromkeys(timeframes) if tf in _FETCH_CONFIG]
    angel_tfs = [tf for tf in timeframes if tf in ("15m", "1h", "4h")]
    yahoo_tfs = [tf for tf in timeframes if tf == "1d"]

    from broker_dispatcher import get_dispatcher
    dispatcher = get_dispatcher()
    splits = dispatcher.split_symbols(list(symbols))

    if angel_tfs and _angel_enabled() and splits["angel"]:
        try:
            angel_syms = splits["angel"]
            if _ensure_tokens(angel_syms):
                _start_bootstrap(angel_syms, "15m", lookback_days=40)
                if any(tf in ("1h", "4h") for tf in angel_tfs):
                    _start_bootstrap(angel_syms, "1h", lookback_days=100)
                tokens = [_ANGEL_TOKENS[s]["token"] for s in angel_syms if s in _ANGEL_TOKENS]
                from angel_feed import get_feed
                get_feed().start(tokens)
                if "15m" in angel_tfs and not _BOOTSTRAP_EVENTS["15m"].is_set():
                    logger.info("Waiting for Angel 15m bootstrap (first run only).")
                    _BOOTSTRAP_EVENTS["15m"].wait(timeout=900)
                if any(tf in ("1h", "4h") for tf in angel_tfs) and not _BOOTSTRAP_EVENTS["1h"].is_set():
                    logger.info("Waiting for Angel 1h bootstrap (first run only).")
                    _BOOTSTRAP_EVENTS["1h"].wait(timeout=900)
                _absorb_feed_candles(angel_syms)
                _detect_gaps(angel_syms)
        except Exception as exc:
            _disable_angel(f"prefetch failure: {exc}")
            # If Angel fails, let Upstox / Yahoo handle them
            splits["upstox"].extend(splits["angel"])

    if angel_tfs and splits["upstox"]:
        try:
            from broker_upstox import get_upstox_client, credentials_available as upstox_ok
            if upstox_ok():
                client = get_upstox_client()
                upstox_keys = []
                for s in splits["upstox"]:
                    info = client.resolve_symbol(s)
                    if info and info.get("instrument_key"):
                        upstox_keys.append(info["instrument_key"])
                from upstox_feed import get_upstox_feed
                get_upstox_feed().start(upstox_keys)
        except Exception as exc:
            logger.debug("Upstox prefetch setup failed: %s", exc)

    if yahoo_tfs:
        _yahoo_prefetch(symbols, yahoo_tfs)


def _merge_upstox_forming(symbol: str, frame: pd.DataFrame) -> pd.DataFrame:
    """Append/union the live Upstox forming 15m candle onto a REST equity frame
    so the Upstox-served half has the same current-session freshness as the
    Angel half. Fully guarded: any index/timezone uncertainty safely no-ops.
    """
    try:
        from broker_upstox import get_upstox_client
        from upstox_feed import get_upstox_feed
        info = get_upstox_client().resolve_symbol(symbol)
        inst_key = info.get("instrument_key") if info else None
        if not inst_key:
            return frame
        fc = get_upstox_feed().get_forming_candle(inst_key)
        if not fc or frame.empty:
            return frame
        start = pd.Timestamp(fc["start"])
        idx_tz = frame.index.tz
        # Normalize the forming timestamp to the frame index's tz convention.
        if idx_tz is not None:
            start = start.tz_localize(idx_tz) if start.tzinfo is None else start.tz_convert(idx_tz)
        elif start.tzinfo is not None:
            start = start.tz_localize(None)
        last = frame.index[-1]
        row = {
            "open": float(fc["open"]), "high": float(fc["high"]),
            "low": float(fc["low"]), "close": float(fc["close"]),
            "volume": float(fc.get("volume", 0.0)),
        }
        frame = frame.copy()
        if start > last:
            frame = pd.concat([frame, pd.DataFrame([row], index=pd.DatetimeIndex([start]))])
        elif start == last:
            frame.loc[last, "high"] = max(float(frame.loc[last, "high"]), row["high"])
            frame.loc[last, "low"] = min(float(frame.loc[last, "low"]), row["low"])
            frame.loc[last, "close"] = row["close"]
            frame.loc[last, "volume"] = max(float(frame.loc[last, "volume"]), row["volume"])
    except Exception as exc:
        logger.debug("Upstox forming merge skipped for %s: %s", symbol, exc)
    return frame


# Timeframes on which the 15-minute Yahoo delay is tolerable. Intraday
# decisions must never be taken on delayed data.
YAHOO_MACRO_TIMEFRAMES = ("4h", "1d")

_TF_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def is_frame_fresh(frame: Optional[pd.DataFrame], timeframe: str,
                   now: Optional[datetime] = None) -> bool:
    """Is this frame recent enough to make a trading decision on?

    The only acceptance test in the read path was `len(frame) >= 50`, and
    _load_pickle_if_fresh accepted a pickle up to three calendar days old. A
    stale frame's last bar is not "forming", so the engine treated a days-old
    bar as the current decision bar and evaluated live stops against it.

    Tolerance is 2 bar-durations during a live session; outside a session the
    newest bar legitimately dates from the last close, so only the staleness
    that would span a whole extra session is rejected.
    """
    if frame is None or frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
        return False
    now = now or _now_ist()
    last = pd.Timestamp(frame.index[-1])
    last = last.tz_localize(_IST_TZ) if last.tzinfo is None else last.tz_convert(_IST_TZ)
    age = (pd.Timestamp(now) - last).total_seconds()

    bar = _TF_SECONDS.get(timeframe, 900)
    try:
        from scanner_engine import get_market_status
        live = bool(get_market_status(now)["market_open"])
    except Exception:
        live = False
    tolerance = (2 * bar) if live else max(4 * bar, 4 * 86400)
    return age <= tolerance


def complete_daily_from_intraday(symbol: str, daily: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """Append/repair recent daily bars using the broker intraday store.

    Yahoo's daily feed lags: for a session that has already closed it can still
    publish the bar with `Close = NaN` (Open/High/Low populated). A row with no
    close is unusable and is correctly dropped, so the daily chart silently
    stops at the PREVIOUS session — measured on 2026-08-08, RELIANCE's 1d chart
    ended Thursday 06-Aug while 15m/1h/4h all correctly showed Friday 07-Aug.
    That is the "1-2 day stale chart".

    The session is already fully described by the Angel intraday store, so the
    daily bar is reconstructed from it rather than waiting for Yahoo. Verified
    against Yahoo's own partial row for 2026-08-07: reconstructed
    O/H/L = 1320.00/1337.00/1316.60 matched to the cent, and supplied the
    missing close of 1334.80.

    Bars are anchored to IST midnight to match the existing daily index.
    """
    if daily is None or daily.empty:
        return daily
    # In-memory store first, then the on-disk pickle. The store is only
    # populated once _bootstrap_interval has run, so a freshly restarted
    # process would otherwise skip the repair and serve a stale daily chart
    # for the whole first cycle.
    intraday = None
    try:
        intraday = _angel_fetch(symbol, "15m")
    except Exception:
        intraday = None
    if intraday is None or intraday.empty:
        try:
            intraday = _load_pickle_if_fresh(symbol, "15m")
        except Exception:
            intraday = None
    if intraday is None or intraday.empty:
        return daily
    intraday = drop_non_session_bars(intraday)
    if intraday is None or intraday.empty:
        return daily

    sessions = (
        intraday.groupby(intraday.index.date)
        .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
             close=("close", "last"), volume=("volume", "sum"))
    )
    if sessions.empty:
        return daily

    sessions.index = pd.DatetimeIndex(
        [pd.Timestamp(d).tz_localize(_IST_TZ) for d in sessions.index]
    )
    # Only trust a session the intraday store actually completed, so a
    # mid-session partial day never masquerades as a closed daily bar.
    try:
        from market_calendar import is_trading_day
        sessions = sessions[[is_trading_day(d.date()) for d in sessions.index]]
    except Exception:
        pass

    last_valid = daily.index[-1]
    missing = sessions[sessions.index > last_valid]
    if missing.empty:
        return daily

    merged = pd.concat([daily, missing[_OHLCV_COLS]])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    # pd.concat can silently downgrade a tz-aware DatetimeIndex to a plain
    # Index when the two frames have subtly different tz representations.
    # Restore it so downstream normalize_ohlcv doesn't reject the frame.
    if not isinstance(merged.index, pd.DatetimeIndex):
        merged.index = pd.to_datetime(merged.index, utc=True).tz_convert(_IST_TZ)
    logger.info(
        "Rebuilt %d daily bar(s) for %s from the intraday store (Yahoo had not "
        "published a usable close): %s",
        len(missing), symbol, ", ".join(str(d.date()) for d in missing.index),
    )
    return merged


def fetch_ohlcv(symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
    """OHLCV frame (IST bar-open index) for one symbol/timeframe, or None."""
    if timeframe not in _FETCH_CONFIG:
        raise ValueError(f"Unsupported timeframe: {timeframe!r}")

    from broker_dispatcher import get_dispatcher
    dispatcher = get_dispatcher()

    if timeframe in ("15m", "1h", "4h"):
        # First check if Angel has cached/bootstrapped data ready when assigned or enabled
        if _angel_enabled():
            try:
                frame = _angel_fetch(symbol, timeframe)
                if frame is not None and len(frame) >= 50:
                    if not is_frame_fresh(frame, timeframe):
                        logger.warning(
                            "Angel store for %s/%s is stale (last bar %s); not trading on it.",
                            symbol, timeframe, frame.index[-1],
                        )
                    else:
                        _LAST_SCAN_SOURCE[(symbol, timeframe)] = "angel"
                        return frame.copy()
            except Exception as exc:
                logger.debug("Angel cache check failed for %s/%s: %s", symbol, timeframe, exc)

        # Delegate to MultiBrokerDispatcher failover (Upstox / Angel REST API)
        try:
            df, source = dispatcher.fetch_ohlcv(symbol, timeframe)
            if df is not None and len(df) >= 50 and is_frame_fresh(df, timeframe):
                _LAST_SCAN_SOURCE[(symbol, timeframe)] = source
                # DQ-2: give the Upstox-served half the same current-session
                # freshness as the Angel half by merging the live forming candle.
                if source == "upstox" and timeframe == "15m":
                    df = _merge_upstox_forming(symbol, df)
                return df.copy()
        except Exception as exc:
            logger.warning("Dispatcher fetch failed %s/%s (%s); using Yahoo", symbol, timeframe, exc)

    # Yahoo is 15 MINUTES DELAYED. It is a legitimate source for the macro
    # timeframes (4h/1d), where a 15-minute lag is a small fraction of a bar,
    # and it is NOT acceptable for a 15m or 1h entry decision — a 15m bar is
    # entirely stale by the time Yahoo publishes it, so the scanner would be
    # entering on prices that no longer exist.
    #
    # This used to be an unconditional final fallback with no timeframe guard,
    # so whenever both brokers failed the engine silently traded delayed data
    # and the operator-facing warning was suppressed at the same moment.
    if timeframe in YAHOO_MACRO_TIMEFRAMES:
        frame = _yahoo_fetch(symbol, timeframe)
        if frame is not None:
            # Yahoo's daily bar for a just-closed session can carry a NaN close,
            # which is dropped as unusable and leaves the daily chart a day or
            # two behind. Rebuild those bars from the intraday store.
            if timeframe == "1d":
                frame = complete_daily_from_intraday(symbol, frame)
            _LAST_SCAN_SOURCE[(symbol, timeframe)] = "yahoo"
        return frame

    _LAST_SCAN_SOURCE[(symbol, timeframe)] = "none"
    logger.warning(
        "No broker data for %s/%s and Yahoo is delayed — refusing to serve a "
        "stale frame for an intraday decision.", symbol, timeframe,
    )
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Yahoo Finance path (fallback + 1d)
# ═══════════════════════════════════════════════════════════════════════════

def _yahoo_prefetch(symbols: list[str], timeframes: list[str]) -> None:
    """Batch-download Yahoo data for the requested timeframes."""
    import yfinance as yf

    now = time.time()
    cache_ttl = _cache_ttl_seconds()

    uncached: list[str] = []
    for timeframe in dict.fromkeys(timeframes):
        if timeframe not in _FETCH_CONFIG:
            continue
        all_fresh = all(
            (entry := _BATCH_CACHE.get((symbol, timeframe))) is not None
            and now - entry[0] < cache_ttl
            for symbol in symbols
        )
        if all_fresh:
            logger.info("Yahoo cache fresh for %s", timeframe)
        else:
            uncached.append(timeframe)

    request_groups: dict[tuple[str, str], list[str]] = {}
    for timeframe in uncached:
        config = _FETCH_CONFIG[timeframe]
        request_groups.setdefault((config["interval"], config["period"]), []).append(timeframe)

    for (interval, period), logical_tfs in request_groups.items():
        cache_timeframes = [
            tf for tf, config in _FETCH_CONFIG.items()
            if config["interval"] == interval and config["period"] == period
        ]
        labels = "/".join(logical_tfs)
        try:
            logger.info("Yahoo batch download %s symbols for %s", len(symbols), labels)
            chunk_size = 50
            df_bulk = pd.DataFrame()
            for i in range(0, len(symbols), chunk_size):
                chunk = symbols[i:i + chunk_size]
                df_chunk = yf.download(
                    tickers=" ".join(chunk),
                    interval=interval,
                    period=period,
                    group_by="ticker",
                    threads=True,
                    # Raw (unadjusted) prices to match Angel/Upstox traded levels;
                    # auto_adjust=True shifted historical levels around ex-dates
                    # and made absolute SL/TP distances inconsistent with the
                    # broker feeds used for the rest of the universe.
                    auto_adjust=False,
                    progress=False,
                )
                if df_chunk is not None and not df_chunk.empty:
                    if not isinstance(df_chunk.columns, pd.MultiIndex):
                        sym = chunk[0] if len(chunk) == 1 else (getattr(df_chunk, "name", None))
                        if not sym:
                            import yfinance.shared as yf_shared
                            failed = set(yf_shared._ERRORS.keys())
                            succeeded = [s for s in chunk if s not in failed]
                            if len(succeeded) == 1:
                                sym = succeeded[0]
                        if sym:
                            df_chunk.columns = pd.MultiIndex.from_product([[sym], df_chunk.columns])
                        else:
                            continue
                    df_bulk = df_chunk if df_bulk.empty else pd.concat([df_bulk, df_chunk], axis=1)
                if i + chunk_size < len(symbols):
                    time.sleep(1)
        except Exception as exc:
            logger.warning("Yahoo bulk download failed for %s: %s", labels, exc)
            continue

        if df_bulk is None or df_bulk.empty:
            continue

        level0 = set(df_bulk.columns.levels[0]) if isinstance(df_bulk.columns, pd.MultiIndex) else set()
        for sym in symbols:
            ticker_key = sym if sym in level0 else None
            if ticker_key is None and sym.endswith(".NS") and sym[:-3] in level0:
                ticker_key = sym[:-3]
            elif ticker_key is None and (sym + ".NS") in level0:
                ticker_key = sym + ".NS"
            if not ticker_key:
                continue
            try:
                sym_df = df_bulk[ticker_key].dropna(how="all").copy()
                if sym_df.empty:
                    continue
                for timeframe in cache_timeframes:
                    _enforce_cache_limit()
                    _BATCH_CACHE[(sym, timeframe)] = (now, sym_df)
                    alt = sym[:-3] if sym.endswith(".NS") else sym + ".NS"
                    _BATCH_CACHE[(alt, timeframe)] = (now, sym_df)
            except Exception:
                pass


def _yahoo_fetch(symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
    import yfinance as yf

    cfg = _FETCH_CONFIG[timeframe]
    now = time.time()
    cache_ttl = _cache_ttl_seconds()
    cache_key = (symbol, timeframe)

    raw = None
    fetched_at = now
    if cache_key in _BATCH_CACHE:
        cached_time, cached_df = _BATCH_CACHE[cache_key]
        if now - cached_time < cache_ttl:
            raw = cached_df.copy()
            fetched_at = cached_time

    if raw is None or raw.empty:
        try:
            raw = yf.Ticker(symbol).history(interval=cfg["interval"], period=cfg["period"], auto_adjust=False)
            fetched_at = time.time()
        except Exception as exc:
            logger.warning("yfinance fetch failed for %s/%s: %s", symbol, timeframe, exc)
            return None

    if raw is None or raw.empty:
        return None

    df = raw.rename(columns={
        "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume",
    })
    keep = [c for c in _OHLCV_COLS if c in df.columns]
    df = df[keep].copy()
    if "volume" not in df.columns:
        df["volume"] = 0.0

    if df.index.tz is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)

    df = df.dropna(subset=["open", "high", "low", "close"])
    bad = (df["high"] < df["low"]) | (df["close"] <= 0) | (df["open"] <= 0)
    if bad.any():
        df = df[~bad]

    if timeframe == "4h":
        df = _resample_intraday(df, "4h", symbol)

    if df.empty or len(df) < 50:
        return None

    # Preserve the ORIGINAL fetch time so TTL measures data age, not access age.
    _enforce_cache_limit()
    _BATCH_CACHE[cache_key] = (fetched_at, df)
    return df.copy()


def _resample_4h(df: pd.DataFrame) -> pd.DataFrame:
    """Backwards-compatible alias used by older tests."""
    return _resample_intraday(df, "4h", None)

def fetch_options_chain(symbol: str, expiry: Optional[str] = None) -> dict:
    """Get full options chain from Upstox instrument master."""
    # Apply F&O ticker aliases
    try:
        from options_engine import _FO_ALIASES
        symbol = _FO_ALIASES.get(symbol.upper().replace('.NS', ''), symbol.upper().replace('.NS', ''))
    except ImportError:
        pass
        
    cache_key = (symbol, expiry)
    now = time.time()
    
    if cache_key in _CHAIN_CACHE:
        cached_time, cached_data = _CHAIN_CACHE[cache_key]
        if now - cached_time < 300.0:  # Cache for 5 minutes
            return cached_data

    try:
        from broker_upstox import get_upstox_client, _parse_expiry
        client = get_upstox_client()
        eq_index, fo_index = client.instrument_map()
        base = symbol.replace(".NS", "").replace("^NSEI", "NIFTY").replace("^NSEBANK", "BANKNIFTY").replace("NIFTY 50", "NIFTY").upper()
        contracts = fo_index.get(base, [])
        if not contracts:
            return {"chain": [], "expiries": [], "spot_price": 0.0}
        
        options = [c for c in contracts if str(c.get("instrument_type", "")).upper() in ("CE", "PE")]
        if not options:
            return {"chain": [], "expiries": [], "spot_price": 0.0}
            
        now_date = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        valid = []
        for c in options:
            exp_dt = _parse_expiry(c.get("expiry"))
            if exp_dt is not None and exp_dt >= now_date:
                valid.append((exp_dt, c))
                
        valid.sort(key=lambda x: x[0])
        if not valid:
            return {"chain": [], "expiries": [], "spot_price": 0.0}
            
        unique_expiries = sorted(list(set(x[0].strftime("%Y-%m-%d") for x in valid)))
            
        if expiry:
            target_expiry = expiry
        else:
            target_expiry = valid[0][0].strftime("%Y-%m-%d")
            
        chain_contracts = []
        instrument_keys = []
        for exp_dt, c in valid:
            if exp_dt.strftime("%Y-%m-%d") == target_expiry:
                chain_contracts.append(c)
                if c.get("instrument_key"):
                    instrument_keys.append(c.get("instrument_key"))
                    
        # Get spot price
        spot_price = 0.0
        spot_ik = eq_index.get(base, {}).get("instrument_key")
        if not spot_ik and base == "NIFTY":
            spot_ik = eq_index.get("NIFTY 50", {}).get("instrument_key")
        if spot_ik:
            instrument_keys.append(spot_ik)
                
        quotes = client.get_quote(instrument_keys)
        if spot_ik and spot_ik in quotes:
            spot_price = float(quotes[spot_ik].get("last_price") or quotes[spot_ik].get("ltp") or 0.0)
        
        chain = []
        for c in chain_contracts:
            ik = c.get("instrument_key")
            q = quotes.get(ik, {}) if ik else {}
            greeks = q.get("option_greeks") or q.get("greeks") or {}
            
            chain.append({
                "instrument_key": ik,
                "trading_symbol": c.get("trading_symbol"),
                "strike": float(c.get("strike_price") or 0.0),
                "type": c.get("instrument_type"),
                "expiry": target_expiry,
                "ltp": float(q.get("last_price") or q.get("ltp") or 0.0),
                "oi": float(q.get("oi") or 0.0),
                "delta": float(greeks.get("delta") or 0.0),
                "theta": float(greeks.get("theta") or 0.0),
                "gamma": float(greeks.get("gamma") or 0.0),
                "vega": float(greeks.get("vega") or 0.0),
                "iv": float(greeks.get("iv") or 0.0),
            })
            
        result = {"chain": chain, "expiries": unique_expiries, "spot_price": spot_price}
        _CHAIN_CACHE[cache_key] = (now, result)
        return result
    except Exception as exc:
        logger.error(f"Error fetching options chain for {symbol}: {exc}")
        return {"chain": [], "expiries": [], "spot_price": 0.0}

def fetch_option_greeks(instrument_key: str) -> dict:
    """Get real-time Greeks from Upstox market quote."""
    try:
        from broker_upstox import get_upstox_client
        client = get_upstox_client()
        quotes = client.get_quote([instrument_key])
        q = quotes.get(instrument_key, {})
        greeks = q.get("option_greeks") or q.get("greeks") or {}
        return {
            "delta": float(greeks.get("delta") or 0.0),
            "theta": float(greeks.get("theta") or 0.0),
            "gamma": float(greeks.get("gamma") or 0.0),
            "vega": float(greeks.get("vega") or 0.0),
            "iv": float(greeks.get("iv") or 0.0),
        }
    except Exception as exc:
        logger.error(f"Error fetching greeks for {instrument_key}: {exc}")
        return {}
