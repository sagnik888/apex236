from __future__ import annotations

import asyncio
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from app import config, indicators as ind
from app.data_sources.yahoo_finance import YahooFinanceSource
from app.engine.scoring import WEIGHT_PROFILES, reason_bullets, score_symbol_timeframe
from app.instrument_master import sync_instruments
from app.market_calendar import IST, market_status
from app.live_quotes import get_live_quotes
from app.main_state import LATEST_SYMBOL_STATES

_SOURCE = YahooFinanceSource()
_CACHE: dict[str, tuple[float, dict[str, pd.DataFrame], dict]] = {}
_LOCKS: dict[str, asyncio.Lock] = {}
TTL = {"5min": 45, "15min": 50, "1h": 110, "1d": 240}
BASE_FOR = {"5min": "5min", "15min": "5min", "30min": "5min", "1h": "1h", "4h": "1h", "1d": "1d", "2d": "1d", "4d": "1d", "1w": "1d"}
GROUP_FOR = {"5min": "intraday", "15min": "intraday", "30min": "intraday", "1h": "intraday", "4h": "swing", "1d": "swing", "2d": "positional", "4d": "positional", "1w": "positional"}
MINUTES = {"5min": 5, "15min": 15, "30min": 30, "1h": 60}
HTF_FOR = {"5min": "15min", "15min": "30min", "30min": "1h", "1h": "4h", "4h": "1d", "1d": "2d", "2d": "4d", "4d": "1w", "1w": None}


def _finite(v):
    try:
        return None if math.isnan(float(v)) or math.isinf(float(v)) else round(float(v), 2)
    except Exception:
        return None


def _seal_direct(df: pd.DataFrame, base: str) -> pd.DataFrame:
    if df.empty:
        return df
    now = datetime.now(IST)
    if market_status(now) != "LIVE":
        return df
    out = df
    if base in MINUTES:
        last_start = out.index[-1].to_pydatetime()
        if last_start.tzinfo is None:
            last_start = last_start.replace(tzinfo=IST)
        if now < last_start.astimezone(IST) + pd.Timedelta(minutes=MINUTES[base]):
            out = out.iloc[:-1]
    elif base == "1d":
        last_dt = pd.Timestamp(out.index[-1])
        if last_dt.tzinfo is None:
            last_dt = last_dt.tz_localize(IST)
        else:
            last_dt = last_dt.tz_convert(IST)
        if last_dt.date() == now.date():
            out = out.iloc[:-1]
    return out


def _seal_target(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Seal derived timeframe buckets after resampling.

    Base feeds are sealed before transformation, but a pair of closed 15m bars
    can still form only half of a 30m bucket, and closed 1h bars can form a
    partial 4h bucket. Derived bars therefore need a second sealing pass.
    """
    if df.empty or market_status() != "LIVE":
        return df
    now = datetime.now(IST)
    out = df
    if tf in ("15min", "30min"):
        start = pd.Timestamp(out.index[-1])
        if start.tzinfo is None:
            start = start.tz_localize(IST)
        else:
            start = start.tz_convert(IST)
        if now < start.to_pydatetime() + pd.Timedelta(minutes=MINUTES[tf]):
            out = out.iloc[:-1]
    elif tf == "4h":
        # 4h resample is right-labelled, so the index itself is the close time.
        end = pd.Timestamp(out.index[-1])
        if end.tzinfo is None:
            end = end.tz_localize(IST)
        else:
            end = end.tz_convert(IST)
        if pd.Timestamp(now) < end:
            out = out.iloc[:-1]
    return out


def _transform(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    if df.empty:
        return df
    if tf in ("5min", "1h", "1d"):
        return df
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    if tf in ("15min", "30min"):
        return df.resample(tf, origin="start_day", offset="9h15min", label="left", closed="left").agg(agg).dropna(subset=["close"])
    if tf == "4h":
        return df.resample("4h", origin="start_day", offset="9h15min", label="right", closed="left").agg(agg).dropna(subset=["close"])
    if tf == "1w":
        return df.resample("W-FRI", label="right", closed="right").agg(agg).dropna(subset=["close"])
    size = 2 if tf == "2d" else 4
    tmp = df.copy().sort_index()
    group = pd.Series(range(len(tmp)), index=tmp.index) // size
    out = tmp.groupby(group.values).agg(agg)
    ends = tmp.groupby(group.values).apply(lambda g: g.index[-1])
    out.index = pd.DatetimeIndex(ends.values)
    if len(tmp) % size:
        out = out.iloc[:-1]
    return out


async def _get_base(base: str, force: bool = False):
    import time
    lock = _LOCKS.setdefault(base, asyncio.Lock())
    async with lock:
        now_mono = time.monotonic()
        cached = _CACHE.get(base)
        if cached and not force and now_mono - cached[0] < TTL[base]:
            return cached[1], cached[2]
        instruments, master_meta = await asyncio.to_thread(sync_instruments, False)
        mapping = {r["symbol"]: r.get("yahoo_ticker") or f'{r["symbol"]}.NS' for r in instruments}
        data = await _SOURCE.get_many_bars(mapping, base, 700)
        data = {s: _seal_direct(df, base) for s, df in data.items() if not df.empty}
        meta = {"instruments": instruments, "master": master_meta}
        _CACHE[base] = (now_mono, data, meta)
        return data, meta


def _freshness(last_time: pd.Timestamp | None, tf: str, source_label: str) -> tuple[str, float | None]:
    if last_time is None:
        return "OFFLINE", None
    now = pd.Timestamp.now(tz="Asia/Kolkata")
    ts = pd.Timestamp(last_time)
    if ts.tzinfo is None:
        ts = ts.tz_localize("Asia/Kolkata")
    else:
        ts = ts.tz_convert("Asia/Kolkata")
    age_min = max(0.0, (now - ts).total_seconds() / 60)
    if market_status() == "LIVE":
        max_age = {"5min": 10, "15min": 25, "30min": 45, "1h": 90, "4h": 300, "1d": 1500, "2d": 4500, "4d": 9000, "1w": 15000}[tf]
        if age_min > max_age:
            return "STALE", round(age_min, 1)
    return source_label, round(age_min, 1)


def _daily_changes(daily: pd.DataFrame) -> dict:
    if daily is None or daily.empty:
        return {}
    c = daily["close"]
    return {f"pct_change_{n}d": _finite(ind.pct_change_n(c, n)) for n in (1, 2, 3, 4, 7, 14, 30, 60)}


def _compact_symbol_states(rows: list[dict]) -> dict[str, dict]:
    """Return the full-universe MTF state without duplicating heavy row payloads.

    These snapshots feed the 5m/15m/30m/1h/4h columns in the dashboard. They
    are intentionally kept out of the WebSocket scan payload so the 15-second
    live-price pulse does not repeatedly send ~237 x 5 indicator records.
    """
    return {
        r["symbol"]: {
            "score": r.get("score"),
            "label": r.get("label"),
            "freshness": r.get("freshness"),
            "last_bar_time": r.get("last_bar_time"),
            "trend_alignment": r.get("trend_alignment"),
        }
        for r in rows
    }


def _score_rows(tf: str, base_data: dict[str, pd.DataFrame], daily_data: dict[str, pd.DataFrame], instruments: list[dict], live_quotes: dict[str, dict], htf_states: dict[str, dict]):
    """CPU-heavy indicator work, designed to run off the asyncio event loop."""
    by_symbol = {r["symbol"]: r for r in instruments}
    rows = []
    failure_symbols: list[dict] = []
    for symbol, info in by_symbol.items():
        raw = base_data.get(symbol)
        if raw is None or raw.empty:
            failure_symbols.append({"symbol": symbol, "reason": "no_base_history"})
            continue
        bars = _seal_target(_transform(raw, tf), tf)
        htf_state = htf_states.get(symbol) or {}
        htf_align = htf_state.get("trend_alignment")
        if htf_state.get("freshness") in ("STALE", "OFFLINE"):
            htf_align = None
        weights = WEIGHT_PROFILES[GROUP_FOR[tf]]
        score = score_symbol_timeframe(
            bars, weights=weights, htf_trend_alignment=htf_align
        )
        if score.get("status") != "ok":
            failure_symbols.append({"symbol": symbol, "reason": score.get("status", "score_failed")})
            continue
        last_ts = bars.index[-1]
        freshness, age = _freshness(last_ts, tf, _SOURCE.freshness_label())
        q = live_quotes.get(info.get("upstox_instrument_key")) or {}
        live_price = q.get("last_price")
        live_ts = q.get("last_trade_time_iso") or q.get("last_trade_time")
        price = live_price if live_price is not None else score["last_close"]
        row = {
            "symbol": symbol, "name": info.get("name", symbol), "lot_size": info.get("lot_size", 0),
            "last_price": round(float(price), 2), "score": score["score"], "label": score["label"],
            "rvol": score["components"].get("rvol"), "adx": score["components"].get("adx"),
            "rsi": score["components"].get("rsi_raw"), "trend_alignment": score["components"].get("trend_alignment"),
            "agreement": score["components"].get("agreement"), "dmi_score": score["components"].get("dmi_score"),
            "freshness": freshness, "data_age_minutes": age,
            "data_source": "upstox_quote+yahoo_bars" if q else _SOURCE.name, "last_bar_time": str(last_ts),
            "last_quote_time": live_ts, "price_is_live": bool(q),
            "reasons": reason_bullets(score["components"], score["label"]),
            **_daily_changes(daily_data.get(symbol)),
        }
        rows.append(row)
    weights = WEIGHT_PROFILES[GROUP_FOR[tf]]
    bulls = sorted((r for r in rows if r["score"] >= weights.bull_threshold), key=lambda r: r["score"], reverse=True)
    bears = sorted((r for r in rows if r["score"] <= weights.bear_threshold), key=lambda r: r["score"])
    return rows, bulls, bears, failure_symbols


async def compute_scan_for_timeframe(tf: str, force: bool = False) -> dict:
    if tf not in BASE_FOR:
        raise ValueError(f"Unsupported timeframe {tf}")
    started = datetime.now(IST)
    base = BASE_FOR[tf]
    base_data, meta = await _get_base(base, force=force)
    daily_data = base_data if base == "1d" else (await _get_base("1d", force=False))[0]
    instruments = meta["instruments"]
    live_keys = [r.get("upstox_instrument_key") for r in instruments if r.get("upstox_instrument_key")]
    live_quotes = await get_live_quotes(live_keys)

    # Pandas/indicator work for ~237 symbols can take noticeable CPU time.
    # Run it in a worker thread so the 15s quote pulse and other scheduler
    # lanes remain responsive instead of being frozen by synchronous scoring.
    htf_tf = HTF_FOR.get(tf)
    htf_states = dict(LATEST_SYMBOL_STATES.get(htf_tf, {})) if htf_tf else {}
    rows, bulls, bears, failure_symbols = await asyncio.to_thread(
        _score_rows, tf, base_data, daily_data, instruments, live_quotes, htf_states
    )
    failures = len(failure_symbols)
    neutral_count = len(rows) - len(bulls) - len(bears)
    generated = datetime.now(IST)
    return {
        "status": "ok" if rows else "no_data", "timeframe": tf, "market_status": market_status(),
        "bullish": bulls[:config.TOP_ROWS], "bearish": bears[:config.TOP_ROWS], "neutral_count": neutral_count,
        "universe_count": len(instruments), "scored_count": len(rows), "failed_count": failures,
        "failed_symbols": failure_symbols,
        "data_source": "upstox_live_quote+yahoo_history" if live_quotes else _SOURCE.name, "source_freshness": "LIVE_PRICE_DELAYED_BARS" if live_quotes else _SOURCE.freshness_label(),
        "instrument_master": meta["master"], "generated_at": generated.isoformat(),
        "refresh_class": ("micro" if tf in {"5min","15min","30min"} else "medium" if tf in {"1h","4h"} else "macro"),
        "scan_duration_seconds": round((generated - started).total_seconds(), 2),
        "warning": ("Live Upstox prices are enabled; technical scores still use closed Yahoo bars and are labelled by bar freshness." if live_quotes else "Yahoo Finance is a delayed fallback. Add UPSTOX_ANALYTICS_TOKEN and set USE_UPSTOX_LIVE=true for exchange-real-time prices."),
        # Private transport field: scheduler removes this before WebSocket/API
        # scan payloads and stores it in LATEST_SYMBOL_STATES.
        "_symbol_states": _compact_symbol_states(rows),
    }
