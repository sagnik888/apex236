"""Scanner engine: orchestrates multi-symbol, multi-timeframe APEX scans."""
from __future__ import annotations

import logging
import math
import os
import time
import uuid
import json
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from datetime import datetime, date as dt_date, time as dt_time, timedelta
from threading import Lock
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

from apex_python_scanner import ApexConfig, ApexScanner, SymbolResult
from data_provider import IST, fetch_ohlcv
from nifty50 import NIFTY236_SYMBOLS, TIMEFRAMES
from database import SessionLocal, Trade, SignalState
from sectors import get_sector
from market_calendar import holidays_for, is_holiday

logger = logging.getLogger(__name__)

IST_TZ = ZoneInfo("Asia/Kolkata")

# Optional ops knob: scan only the first N symbols (integration testing).
_symbol_limit = int(os.getenv("APEX_SYMBOL_LIMIT", "0") or "0")
SCAN_SYMBOLS: list[str] = NIFTY236_SYMBOLS[:_symbol_limit] if _symbol_limit > 0 else NIFTY236_SYMBOLS


def scan_worker_count() -> int:
    return 3


# How often each timeframe is rescanned, in 60-second scan cycles. Smaller
# timeframes refresh every cycle because their bars close fastest; larger ones
# are staggered so they never all land on the same cycle, which spreads the load
# instead of spiking it. Every timeframe is still refreshed well inside the
# 10-15 minute freshness requirement.
# (period, offset) in cycles. The offsets are chosen so the higher timeframes
# never land on the same cycle: without them cycle 30 is divisible by 5, 10 AND
# 15, so one cycle a half-hour carried every timeframe at once.
_TF_SCAN_SCHEDULE: dict[str, tuple[int, int]] = {
    "15m": (1, 0),    # every minute
    "1h": (1, 0),     # every minute (no API cost, driven by local websocket)
    "4h": (2, 1),     # every 2 minutes (no API cost, resampled locally)
    "1d": (5, 2),     # every 5 minutes (external Yahoo API fetch, must protect rate limits)
}


def timeframes_due(cycle: int, enabled: Optional[list[str]] = None) -> list[str]:
    """Which timeframes to scan on this cycle.

    Staggered rather than "all higher timeframes together every 5th cycle", so
    a single cycle never has to carry 4x the work.
    """
    allowed = set(enabled or TIMEFRAMES)
    due = []
    for tf in TIMEFRAMES:
        if tf not in allowed:
            continue
        period, offset = _TF_SCAN_SCHEDULE.get(tf, (1, 0))
        if cycle % period == offset % period:
            due.append(tf)
    # The fastest enabled timeframe must never be skipped.
    if not due:
        fastest = next((tf for tf in TIMEFRAMES if tf in allowed), None)
        due = [fastest] if fastest else []
    return due


def active_scan_symbols() -> list[str]:
    """Symbols to scan right now, honouring the operator's index-tier selection.

    SCAN_SYMBOLS is fixed at import; this is evaluated per call so unticking a
    tier in Settings takes effect on the next cycle without a restart.
    Fails open to the full universe — a bad selection must never silently
    shrink the scan to nothing.
    """
    try:
        from index_classification import symbols_for
        from settings_store import get_settings
        picked = symbols_for(get_settings().get("enabled_indices"), universe=SCAN_SYMBOLS)
        return picked or list(SCAN_SYMBOLS)
    except Exception as exc:
        logger.warning("Index selection unavailable (%s); scanning the full universe.", exc)
        return list(SCAN_SYMBOLS)

# ─── NSE Session Helpers ──────────────────────────────────────────────────────

NSE_OPEN  = dt_time(9, 15)
NSE_CLOSE = dt_time(15, 30)
# The real NSE pre-open call auction runs 09:00-09:15. Anything earlier is
# simply overnight: treating 00:00-09:15 as PRE_OPEN made scan_interval_secs
# return 60s from midnight, so the engine ran hundreds of full 236-symbol
# four-timeframe scans a night against unchanged bars — burning broker rate
# limit and re-running the DB projection (including its close paths) on stale data.
NSE_PRE_OPEN = dt_time(9, 0)

# Backwards-compatible alias; the authoritative list lives in market_calendar.
NSE_HOLIDAYS_2026: set[dt_date] = holidays_for(2026)


def ist_now() -> datetime:
    return datetime.now(IST_TZ)


def _epoch_ist(ts) -> int:
    """UTC epoch seconds for a bar timestamp, treating naive values as IST.

    Bar timestamps are IST throughout this system, but pandas interprets a
    tz-NAIVE Timestamp as UTC in .timestamp(). A frame that lost its tzinfo
    anywhere upstream would therefore emit an epoch 19800s (5h30m) too large,
    and the chart would silently plot every candle 5.5 hours late — the exact
    class of bug that makes a post-mortem of a losing trade read against the
    wrong clock. Localise explicitly rather than relying on every producer.
    """
    t = pd.Timestamp(ts)
    t = t.tz_localize(IST_TZ) if t.tzinfo is None else t.tz_convert(IST_TZ)
    return int(t.timestamp())


def get_market_status(now: Optional[datetime] = None) -> dict:
    """Return NSE cash-market session status in Asia/Kolkata time.

    ``now`` is injectable so session boundaries can be verified without
    changing the machine clock. Naive values are interpreted as IST.
    """
    now = now or ist_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST_TZ)
    else:
        now = now.astimezone(IST_TZ)
    today = now.date()
    t = now.time().replace(tzinfo=None)

    holiday_today = is_holiday(today)
    from market_calendar import is_mock_session, is_trading_day
    trading_today = is_trading_day(today)
    is_open_window = NSE_OPEN <= t <= NSE_CLOSE
    market_open = trading_today and is_open_window
    # Derive the label from `trading_today` so it can never contradict
    # `market_open`. Keying the weekend branch off the raw weekday reported
    # "WEEKEND" for a real Saturday budget session while market_open was True.
    if not trading_today:
        if is_mock_session(today):
            status = "MOCK_SESSION"
        elif holiday_today:
            status = "HOLIDAY"
        else:
            status = "WEEKEND"
    elif t < NSE_PRE_OPEN:
        status = "CLOSED"      # overnight, not pre-open
    elif t < NSE_OPEN:
        status = "PRE_OPEN"    # the real 09:00-09:15 call auction
    elif t > NSE_CLOSE:
        status = "CLOSED"
    else:
        status = "OPEN"

    # Calculate next open / next close
    next_open_str: Optional[str] = None
    next_close_str: Optional[str] = None
    if market_open:
        close_today = datetime(now.year, now.month, now.day, 15, 30, tzinfo=IST_TZ)
        next_close_str = close_today.isoformat()
    else:
        # Find next trading day
        candidate = today
        for _ in range(10):
            candidate_dt = datetime(candidate.year, candidate.month, candidate.day, 9, 15, tzinfo=IST_TZ)
            if candidate == today and t > NSE_CLOSE:
                candidate = candidate + timedelta(days=1)
                continue
            # is_trading_day, not a raw weekday test, so a real special session
            # (budget Saturday) counts and a mock/DR Saturday does not.
            if is_trading_day(candidate):
                next_open_str = candidate_dt.isoformat()
                break
            candidate = candidate + timedelta(days=1)

    return {
        "session_status": status,
        "market_open": market_open,
        "nse_time": now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "next_open": next_open_str,
        "next_close": next_close_str,
    }


def scan_interval_secs(now: Optional[datetime] = None) -> int:
    """Return the recommended seconds to wait before the next scan."""
    import os
    override = os.getenv("APEX_SCAN_INTERVAL_SECS")
    if override:
        return int(override)
        
    ms = get_market_status(now)
    if ms["market_open"]:
        return 60     # live 15m signal refresh
    if ms["session_status"] == "PRE_OPEN":
        return 60     # do not sleep through the 09:15 transition
    return 3600       # 1 hour outside market (swing signals still matter)


# ─── Per-timeframe APEX configs ───────────────────────────────────────────────

# entry_delay_bars=1: signal evaluates on candle close, entry fills at the NEXT
# candle's open. The previous 0 reproduced Pine's same-bar lookahead (the top
# finding in both audits) and inflated every backtest stat shown in the UI.
# strict_ohlcv=False: one bad provider tick must drop the row with a warning,
# not silently knock the whole symbol out of the scan while stale results
# stay published.
_CONFIGS: dict[str, ApexConfig] = {
    "15m": ApexConfig(
        min_score=65.0,
        conflict_margin=15.0,
        use_htf=True,
        entry_delay_bars=1,
        realistic_fills=True,
        allow_entry_on_last_bar=False,  # wait for the bar to close before entry
        act_on_forming_bar=False,       # no signal/exit decision on a forming bar
        round_trip_cost_pct=0.182,      # report NET-of-cost stats
        use_session=True,
        enforce_market_hours=True,
        max_input_bars=400,
        keep_full_history=False,
        fixed_sl_pct=0.5,  # 0.5% for intraday as requested
        strict_ohlcv=False,
    ),
    "1h": ApexConfig(
        min_score=63.0,
        conflict_margin=14.0,
        use_htf=True,
        entry_delay_bars=1,
        realistic_fills=True,
        allow_entry_on_last_bar=False,
        act_on_forming_bar=False,
        round_trip_cost_pct=0.182,
        use_session=False,
        max_input_bars=400,
        keep_full_history=False,
        fixed_sl_pct=0.0,  # No fixed SL hardcap - always ATR-based
        strict_ohlcv=False,
    ),
    "4h": ApexConfig(
        min_score=60.0,
        conflict_margin=12.0,
        use_htf=True,
        entry_delay_bars=1,
        realistic_fills=True,
        allow_entry_on_last_bar=False,
        act_on_forming_bar=False,
        round_trip_cost_pct=0.182,
        use_session=False,
        max_input_bars=350,
        keep_full_history=False,
        fixed_sl_pct=0.0,  # No fixed SL hardcap - always ATR-based
        strict_ohlcv=False,
    ),
    "1d": ApexConfig(
        min_score=58.0,
        conflict_margin=10.0,
        use_htf=True,
        entry_delay_bars=1,
        realistic_fills=True,
        allow_entry_on_last_bar=False,
        # 1d keeps act_on_forming_bar=True: deferring a daily signal a full day
        # is worse than the small repaint risk on the last (Yahoo EOD) bar.
        round_trip_cost_pct=0.182,
        use_session=False,
        max_input_bars=300,
        keep_full_history=False,
        fixed_sl_pct=0.0,  # No fixed SL hardcap - always ATR-based
        strict_ohlcv=False,
    ),
}

# The per-timeframe gate ladder, stated as an EXPLICIT offset from the user's
# setting rather than hidden behind an equality test against DEFAULTS. Faster
# timeframes are noisier and so demand a higher score and tolerate a narrower
# bull/bear spread. With the shipped defaults (min_score 60, conflict_margin 20)
# these reproduce the original 65/63/60/58 and 15/14/12/10 baselines exactly,
# and any change the operator makes now shifts the whole ladder instead of
# being silently discarded.
_TF_MIN_SCORE_OFFSET: dict[str, float] = {"15m": 5.0, "1h": 3.0, "4h": 0.0, "1d": -2.0}
_TF_CONFLICT_OFFSET: dict[str, float] = {"15m": -5.0, "1h": -6.0, "4h": -8.0, "1d": -10.0}
# Cooldown is measured in BARS, so the same number means very different things
# per timeframe: 1 bar is 15 minutes on 15m but a whole day on 1d. A coarse
# timeframe needs only a single candle of separation between signals; a fast one
# needs a few to avoid re-entering the same intrabar whipsaw. Same contract as
# the ladders above — the operator's setting is the base and always applies.
_TF_COOLDOWN_OFFSET: dict[str, int] = {"15m": 2, "1h": 1, "4h": 0, "1d": 0}


def build_configs() -> dict[str, ApexConfig]:
    """Apply user settings (settings_store) on top of the per-TF baselines."""
    import copy
    from settings_store import get_settings

    from settings_store import DEFAULTS as _SETTINGS_DEFAULTS
    s = get_settings()
    configs: dict[str, ApexConfig] = {}
    for tf, base in _CONFIGS.items():
        cfg = copy.copy(base)  # dataclass with slots; shallow copy is fine
        # These three used to be applied ONLY when the persisted value differed
        # from settings_store.DEFAULTS, so that the per-timeframe baseline
        # ladder survived. That silently discarded the operator's choice
        # whenever it happened to equal the default: apex_settings.json holds
        # min_score=60 / conflict_margin=20 / signal_cooldown=1, which ARE the
        # defaults, so the engine ran the 65/15/5 baselines while the UI showed
        # 60/20/1. Apply them unconditionally like every other field; the
        # per-timeframe ladder is expressed as an explicit offset instead.
        cfg.min_score = min(100.0, max(0.0, float(s["min_score"]) + _TF_MIN_SCORE_OFFSET.get(tf, 0.0)))
        cfg.conflict_margin = max(0.0, float(s["conflict_margin"]) + _TF_CONFLICT_OFFSET.get(tf, 0.0))
        cfg.signal_cooldown = max(0, int(s["signal_cooldown"]) + _TF_COOLDOWN_OFFSET.get(tf, 0))
        cfg.min_adx = float(s["min_adx"])
        cfg.use_htf = bool(s["use_htf"])
        cfg.atr_mult = float(s["atr_mult"])
        if s["sl_mode"] == "fixed":
            cfg.fixed_sl_pct = float(s["fixed_sl_pct"])
            # Without this the fixed stop is only honoured on bars that are
            # NEITHER trending NOR high-volatility — but the entry gate itself
            # requires adx >= min_adx, i.e. is_trending, so "fixed" mode was
            # unreachable by construction and every trade still got an ATR stop.
            cfg.force_fixed_sl = True
        # Both of these were absent, so the settings values were inert: the
        # engine ran the ApexConfig defaults (slippage 0.05 vs the configured
        # 0.1, daily loss cap 3.0 vs the configured 2.0).
        cfg.slippage_pct = float(s["slippage_pct"])
        cfg.daily_max_loss_pct = float(s["daily_max_loss_pct"])
        if s["target_mode"] == "fixed":
            cfg.fixed_tp_pct = float(s["fixed_tp_pct"])
        else:
            cfg.fixed_tp_pct = 0.0
            cfg.t1_r, cfg.t2_r, cfg.t3_r = float(s["t1_r"]), float(s["t2_r"]), float(s["t3_r"])
        cfg.exit_at_t1 = bool(s["exit_at_t1"])
        cfg.use_trail = bool(s["use_trail"])
        cfg.trail_start_r = float(s["trail_start_r"])
        cfg.trail_mult = float(s["trail_mult"])
        cfg.lock_at_t1 = bool(s["lock_at_t1"])
        cfg.exit_confirmation_bars = int(s["exit_confirmation_bars"])
        cfg.max_consecutive_losses = int(s["max_consecutive_losses"])
        cfg.circuit_pause_bars = int(s["circuit_pause_bars"])
        cfg.use_session = bool(s["use_session"])
        cfg.block_open_noise = bool(s["block_open_noise"])
        cfg.block_close_noise = bool(s["block_close_noise"])
        if "enable_options" in s:
            cfg.enable_options = bool(s["enable_options"])
        if "strike_mode" in s:
            cfg.strike_mode = str(s["strike_mode"])
        if "trade_options_intraday" in s:
            cfg.trade_options_intraday = bool(s["trade_options_intraday"])
        if "options_broker" in s:
            cfg.options_broker = str(s["options_broker"])
        if "options_stop_mode" in s:
            cfg.options_stop_mode = str(s["options_stop_mode"])
        if "trade_style" in s:
            cfg.trade_style = str(s["trade_style"])
        cfg.timeframe = tf  # let run_symbol apply timeframe-aware style rules
        # ML-01 FIX: Wire the trained XGBoost model so it actually loads.
        # score_model_path was always "" so the AI scoring block never activated.
        from pathlib import Path
        _model_path = Path(__file__).parent / "apex_score_model.json"
        if _model_path.exists():
            cfg.score_model_path = str(_model_path)
        cfg.validate()
        configs[tf] = cfg
    return configs


# Timeframes each trade style scans, so the style selection "aligns with the
# timeframe selection" the user makes in the UI.
TRADE_STYLE_TIMEFRAMES: dict[str, list[str]] = {
    "intraday": ["15m", "1h"],
    "btst": ["15m", "1h"],
    "swing": ["1h", "4h", "1d"],
    "all": ["15m", "1h", "4h", "1d"],
}


def timeframes_for_style(style: str, enabled: list[str]) -> list[str]:
    """Intersect the user's enabled timeframes with those the style supports,
    preserving canonical order. Falls back to `enabled` if the intersection is
    empty so a mismatched selection never yields an empty scan."""
    allowed = set(TRADE_STYLE_TIMEFRAMES.get(style, TIMEFRAMES))
    aligned = [tf for tf in TIMEFRAMES if tf in allowed and tf in set(enabled)]
    return aligned or [tf for tf in TIMEFRAMES if tf in set(enabled)] or list(TIMEFRAMES)


# ─── Top-level Helpers & Formatting ───────────────────────────────────────────

_EXPECTED_DURATION_HRS: dict[str, float] = {
    "15m": 4.0,
    "1h": 24.0,
    "4h": 72.0,
    "1d": 240.0,
}


def _ts(val: Any) -> Optional[str]:
    """Convert a timestamp/datetime/string to ISO string format or return None."""
    if val is None or pd.isna(val) or val == "" or val == "—":
        return None
    if isinstance(val, str):
        return val
    if hasattr(val, "isoformat"):
        return val.isoformat()
    try:
        return str(pd.to_datetime(val).isoformat())
    except Exception:
        return str(val)


def _safe(val: Any) -> Optional[float]:
    """Safely convert value to float, returning None if invalid/nan/inf."""
    if val is None or pd.isna(val) or val == "" or val == "—":
        return None
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, 4)
    except (ValueError, TypeError):
        return None


def _tick(val: Any) -> Optional[float]:
    """Format price value to 2 decimal places."""
    f = _safe(val)
    return round(f, 2) if f is not None else None


def _first_num(*args: Any) -> Optional[float]:
    """Return the first positive valid float from arguments."""
    for arg in args:
        v = _safe(arg)
        if v is not None and v > 0:
            return v
    return None


def _daily_move_pct(result: SymbolResult) -> Optional[float]:
    """Extract or calculate daily move percentage from SymbolResult."""
    if not result or not result.latest:
        return None
    val = _safe(result.latest.get("daily_move_pct"))
    if val is not None:
        return val
    c = _safe(result.latest.get("close"))
    o = _safe(result.latest.get("open"))
    if c is not None and o is not None and o > 0:
        return round((c - o) / o * 100, 2)
    # Fallback: use prev_close if open is not available
    pc = _safe(result.latest.get("prev_close")) or _safe(result.latest.get("previous_close"))
    if c is not None and pc is not None and pc > 0:
        return round((c - pc) / pc * 100, 2)
    return None


def _trade_age_hrs(entry_ts: Any) -> Optional[float]:
    """Return elapsed MARKET hours since entry_ts in Asia/Kolkata time."""
    if entry_ts is None or pd.isna(entry_ts) or entry_ts == "" or entry_ts == "—":
        return None
    try:
        now_dt = datetime.now(IST_TZ)
        dt = pd.to_datetime(entry_ts)
        if dt.tz is None:
            dt = dt.tz_localize(IST_TZ)
        else:
            dt = dt.tz_convert(IST_TZ)
            
        if dt > now_dt:
            return 0.0
            
        market_open = pd.Timedelta(hours=9, minutes=15)
        market_close = pd.Timedelta(hours=15, minutes=30)
        
        # Calculate business days between signal and now
        bdays = pd.bdate_range(dt.date(), now_dt.date())
        if len(bdays) == 0:
            return 0.0
            
        total_seconds = 0.0
        for day in bdays:
            day_start = pd.Timestamp(day) + market_open
            day_end = pd.Timestamp(day) + market_close
            
            day_start = day_start.tz_localize(IST_TZ)
            day_end = day_end.tz_localize(IST_TZ)
            
            overlap_start = max(dt, day_start)
            overlap_end = min(now_dt, day_end)
            
            if overlap_start < overlap_end:
                total_seconds += (overlap_end - overlap_start).total_seconds()
                
        return round(total_seconds / 3600.0, 1)
    except Exception:
        return None


def _eta_hrs(tf: str, age_hrs: Optional[float]) -> Optional[float]:
    """Return expected remaining duration in hours for timeframe."""
    exp = _EXPECTED_DURATION_HRS.get(tf, 24.0)
    if age_hrs is None:
        return exp
    return round(max(0.0, exp - age_hrs), 1)


def _trade_type(
    tf: str,
    entry_ts: Any,
    direction: str,
    expected_duration: float,
    exit_ts: Any = None,
) -> str:
    """Classify trade as INTRADAY or SWING."""
    if tf in ("4h", "1d"):
        return "SWING"
    if entry_ts is not None and not pd.isna(entry_ts) and entry_ts != "—":
        try:
            dt_in = pd.to_datetime(entry_ts)
            if exit_ts is not None and not pd.isna(exit_ts) and exit_ts != "—":
                dt_out = pd.to_datetime(exit_ts)
                if dt_out.date() != dt_in.date():
                    return "BTST"
            else:
                now_dt = datetime.now(IST_TZ)
                if now_dt.date() != dt_in.date():
                    return "BTST"
        except Exception:
            pass
    return "INTRADAY"


def _signal_score_from_result(result: SymbolResult) -> float:
    """Extract score from result and decay exponentially by bars_since_signal."""
    if not result or not result.latest:
        return 0.0

    if result.active_trade:
        raw = result.active_trade.signal_score
        bars_since = max(0, len(result.frame) - result.active_trade.entry_bar)
    elif result.pending_order:
        raw = float(result.pending_order.get("score", 0.0))
        bars_since = max(0, len(result.frame) - result.pending_order.get("signal_bar", len(result.frame)))
    else:
        raw = _safe(result.latest.get("signal_score")) or 0.0
        bars_since = 0

    if bars_since > 0:
        return round(raw * (0.95 ** bars_since), 1)
    return round(raw, 1)


_TF_BAR_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def _last_bar_is_forming(df: Optional[pd.DataFrame], tf: str) -> bool:
    """True if the frame's last bar has not yet closed (still in progress).

    Erring toward True during the live session is safe: it only defers a
    signal/exit by one scan cycle rather than acting on partial-bar data.
    """
    secs = _TF_BAR_SECONDS.get(tf)
    if not secs or df is None or df.empty:
        return False
    try:
        last_open = pd.Timestamp(df.index[-1])
        if last_open.tzinfo is None:
            last_open = last_open.tz_localize("Asia/Kolkata")
        else:
            last_open = last_open.tz_convert("Asia/Kolkata")
        close_time = last_open + pd.Timedelta(seconds=secs)
        now = pd.Timestamp(ist_now())
        if now.tzinfo is None:
            now = now.tz_localize("Asia/Kolkata")
        else:
            now = now.tz_convert("Asia/Kolkata")
            
        # Clamp intraday close times to market close (15:30 IST)
        if secs < 86400:
            market_close = last_open.replace(hour=15, minute=30, second=0, microsecond=0)
            if close_time > market_close:
                close_time = market_close
                
        return close_time > now
    except Exception:
        return False


_WORKER_CONFIGS_CACHE: Optional[dict[str, ApexConfig]] = None
_WORKER_SETTINGS_REV: int = 0

def _scan_preloaded(
    symbol: str,
    tf: str,
    df: pd.DataFrame,
    settings_rev: int = 0,
    live_option_ltp: float = math.nan,
) -> Optional[SymbolResult]:
    """Top-level worker function executed inside ProcessPoolExecutor."""
    global _WORKER_CONFIGS_CACHE, _WORKER_SETTINGS_REV
    if df is None or df.empty or len(df) < 50:
        logger.info(
            "Skipping %s/%s: insufficient history (%s bars)",
            symbol, tf, 0 if df is None else len(df),
        )
        return None
    try:
        if _WORKER_CONFIGS_CACHE is None or _WORKER_SETTINGS_REV != settings_rev:
            _WORKER_CONFIGS_CACHE = build_configs()
            _WORKER_SETTINGS_REV = settings_rev
        cfg = _WORKER_CONFIGS_CACHE.get(tf)
        if cfg is None:
            return None
        scanner = ApexScanner(cfg)
        return scanner.run_symbol(symbol, df, last_bar_is_forming=_last_bar_is_forming(df, tf), live_option_ltp=live_option_ltp)
    except Exception as exc:
        logger.warning(f"Worker scan error for {symbol}/{tf}: {exc}")
        return None


class ScannerEngine:
    """Maintains scanner state across all symbols and timeframes."""

    def __init__(self) -> None:
        self._results: dict[str, dict[str, SymbolResult]] = {tf: {} for tf in TIMEFRAMES}
        self._scanning: bool = False
        self._last_scan: Optional[datetime] = None
        self._scan_errors: int = 0
        self._scan_count: int = 0
        self._scan_latency_ms: Optional[float] = None
        self._scan_started_at: Optional[datetime] = None
        self._scan_state_lock = Lock()
        self._results_lock = Lock()
        self._events_lock = Lock()
        self._pending_events: list[dict] = []   # notification events from last scan
        self._analytics_cache: dict[tuple[int, Optional[str]], dict] = {}
        self._signals_cache: Optional[dict] = None
        self._signals_lock = Lock()
        self._trades_cache: Optional[dict] = None
        self._leaderboard_cache: Optional[dict] = None
        self._api_cache_lock = Lock()
        self._chart_cache: dict[tuple[str, str], tuple[int, dict, str]] = {}
        self._chart_pending: set[tuple[str, str]] = set()
        self._chart_lock = Lock()
        self._chart_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="apex-chart")
        self._scan_executor: Optional[ThreadPoolExecutor] = None

        # Keep analytics route latency deterministic even while the first scan
        # is still building. These empty snapshots are replaced atomically when
        # a complete scan generation is ready.
        for tenure in ("1d", "7d", "30d", "90d", "180d", "365d"):
            self.get_analytics(tenure)

    def _get_scan_executor(self) -> ThreadPoolExecutor:
        if self._scan_executor is None:
            self._scan_executor = ThreadPoolExecutor(max_workers=4)
        return self._scan_executor

    def shutdown(self) -> None:
        """Release background executors during an orderly API shutdown."""
        self._chart_executor.shutdown(wait=False, cancel_futures=True)
        if self._scan_executor is not None:
            self._scan_executor.shutdown(wait=False, cancel_futures=True)

    # ── State diffing for notifications ───────────────────────────────────────

    def _snapshot_states(self) -> dict:
        """Snapshot current signal states for diffing after next scan."""
        snap: dict = {}
        with self._results_lock:
            results_snap = {t: self._results.get(t, {}).copy() for t in TIMEFRAMES}
            
        for tf in TIMEFRAMES:
            for sym, result in results_snap[tf].items():
                lt = result.latest
                at = result.active_trade
                snap[(sym, tf)] = {
                    "signal":    str(lt.get("signal", "")),
                    "state":     str(lt.get("state", "")),
                    "direction": str(lt.get("active_direction", "")),
                    "t1_hit":    bool(at.t1_hit) if at else False,
                    "t2_hit":    bool(at.t2_hit) if at else False,
                    "t3_hit":    bool(at.t3_hit) if at else False,
                    "score":     _signal_score_from_result(result),
                    "tsl":       _safe(at.tsl) if at else None,
                    "trade_type": _trade_type(tf, at.entry_time or at.signal_time, str(lt.get("active_direction", "")), _EXPECTED_DURATION_HRS[tf]) if at else _trade_type(tf, lt.get("timestamp"), str(lt.get("signal", "")), _EXPECTED_DURATION_HRS[tf]),
                }
        return snap

    def _diff_states(self, prev: dict) -> list[dict]:
        """Compare prev vs current states; return list of notification events."""
        events: list[dict] = []
        now_str = _ts(self._last_scan) or ""
        
        with self._results_lock:
            results_snap = {t: self._results.get(t, {}).copy() for t in TIMEFRAMES}

        for tf in TIMEFRAMES:
            for sym, result in results_snap[tf].items():
                lt = result.latest
                at = result.active_trade
                curr_sig   = str(lt.get("signal", ""))
                curr_state = str(lt.get("state", ""))
                curr_dir   = str(lt.get("active_direction", ""))
                curr_score = _signal_score_from_result(result)
                curr_t1 = bool(at.t1_hit) if at else False
                curr_t2 = bool(at.t2_hit) if at else False
                curr_t3 = bool(at.t3_hit) if at else False
                curr_tt = _trade_type(tf, at.entry_time or at.signal_time, curr_dir, _EXPECTED_DURATION_HRS[tf]) if at else _trade_type(tf, lt.get("timestamp"), curr_sig, _EXPECTED_DURATION_HRS[tf])

                old = prev.get((sym, tf))
                if old is None:
                    old = {"signal": "", "state": "", "direction": "", "t1_hit": False, "t2_hit": False, "t3_hit": False, "score": 0.0, "tsl": None, "trade_type": curr_tt}


                curr_tsl = _safe(at.tsl) if at else None

                # New BUY / SELL signal generated this scan
                if curr_sig in ("BUY", "SELL") and old["signal"] != curr_sig:
                    display_dir = curr_sig
                    close = _safe(lt.get("close")) or 0.0
                    sl1   = _tick(_first_num(lt.get("sl1"), lt.get("planned_sl1")))
                    tp1   = _tick(_first_num(lt.get("tp1"), lt.get("planned_tp1")))
                    events.append({
                        "type":      "new_signal",
                        "symbol":    sym,
                        "timeframe": tf,
                        "direction": display_dir,
                        "score":     round(curr_score, 1),
                        "setup":     str(lt.get("setup", "")),
                        "price":     close,
                        "sl1":       sl1,
                        "tp1":       tp1,
                        "timestamp": now_str,
                    })

                # Became ACTIVE (trade triggered)
                if curr_state == "ACTIVE" and old["state"] != "ACTIVE":
                    display_dir = "BUY" if curr_dir == "LONG" else "SELL"
                    entry = _safe(at.entry_price) if at else None
                    sl1   = _safe(at.sl1) if at else None
                    tp1   = _safe(at.tp1) if at else None
                    events.append({
                        "type":        "trade_triggered",
                        "symbol":      sym,
                        "timeframe":   tf,
                        "direction":   display_dir,
                        "entry_price": entry,
                        "sl1":         sl1,
                        "tp1":         tp1,
                        "setup":       str(at.setup) if at else "",
                        "timestamp":   now_str,
                    })

                # Target 1 hit
                if curr_t1 and not old["t1_hit"]:
                    events.append({
                        "type": "target_hit", "target": "T1",
                        "symbol": sym, "timeframe": tf,
                        "price": _safe(at.tp1) if at else None,
                        "timestamp": now_str,
                    })
                # Target 2 hit
                if curr_t2 and not old["t2_hit"]:
                    events.append({
                        "type": "target_hit", "target": "T2",
                        "symbol": sym, "timeframe": tf,
                        "price": _safe(at.tp2) if at else None,
                        "timestamp": now_str,
                    })
                # Target 3 hit
                if curr_t3 and not old["t3_hit"]:
                    events.append({
                        "type": "target_hit", "target": "T3",
                        "symbol": sym, "timeframe": tf,
                        "price": _safe(at.tp3) if at else None,
                        "timestamp": now_str,
                    })

                # TSL moved (Tracking Trailing)
                if old["state"] == "ACTIVE" and curr_state == "ACTIVE" and curr_tsl is not None and old.get("tsl") is not None:
                    is_long = curr_dir == "LONG"
                    if (is_long and curr_tsl > old["tsl"]) or (not is_long and curr_tsl < old["tsl"]):
                        events.append({
                            "type":      "tsl_update",
                            "symbol":    sym,
                            "timeframe": tf,
                            "direction": "BUY" if is_long else "SELL",
                            "price":     curr_tsl,
                            "timestamp": now_str,
                        })

                # Trade Type Transition (e.g. Intraday -> BTST)
                if old["state"] in ("ACTIVE", "PENDING") and curr_state in ("ACTIVE", "PENDING"):
                    old_tt = old.get("trade_type")
                    if old_tt and curr_tt and old_tt != curr_tt and old_tt != "Swing":
                        events.append({
                            "type":      "trade_transition",
                            "symbol":    sym,
                            "timeframe": tf,
                            "old_type":  old_tt,
                            "new_type":  curr_tt,
                            "timestamp": now_str,
                        })

                # SL hit: was ACTIVE, now FLAT with a loss
                if old["state"] == "ACTIVE" and curr_state == "FLAT":
                    close = _safe(lt.get("close")) or 0.0
                    exit_reason = str(lt.get("exit_reason", ""))
                    if "TSL" in exit_reason or "Profit Locked" in exit_reason:
                        events.append({
                            "type":      "tsl_hit",
                            "symbol":    sym,
                            "timeframe": tf,
                            "direction": "BUY" if old["direction"] == "LONG" else "SELL",
                            "price":     close,
                            "timestamp": now_str,
                        })
                    else:
                        events.append({
                            "type":      "sl_hit",
                            "symbol":    sym,
                            "timeframe": tf,
                            "direction": "BUY" if old["direction"] == "LONG" else "SELL",
                            "price":     close,
                            "reason":    exit_reason,
                            "timestamp": now_str,
                        })

        return events

    def pop_pending_events(self) -> list[dict]:
        """Return and clear pending notification events."""
        with self._events_lock:
            evts = self._pending_events
            self._pending_events = []
            return evts

    # ── Scan ──────────────────────────────────────────────────────────────────
    def run_all_scans(self, timeframes: Optional[list[str]] = None) -> None:
        """Fetch data and scan all configured symbols on all/given timeframes."""
        # Admit one scan atomically so overlapping background/manual requests
        # cannot both start or inflate the telemetry counter.
        with self._scan_state_lock:
            if self._scanning:
                logger.info("Scan already running – skip")
                return
            self._scanning = True
            self._scan_errors = 0
            self._scan_count += 1
            self._scan_started_at = ist_now()
        try:
            scan_started_monotonic = time.monotonic()
            from settings_store import get_settings, revision as settings_revision
            _settings = get_settings()
            enabled = _settings["enabled_timeframes"]
            trade_style = str(_settings.get("trade_style", "all"))
            tfs_to_scan = timeframes or [tf for tf in TIMEFRAMES if tf in enabled] or TIMEFRAMES
            # FIX DATA-03: Orphaned Trades - scan timeframes with active positions even if disabled.
            with self._results_lock:
                for tf in TIMEFRAMES:
                    if tf not in tfs_to_scan:
                        for sym, result in self._results.get(tf, {}).items():
                            if str(result.latest.get("state", "")) in ("ACTIVE", "PENDING"):
                                tfs_to_scan.append(tf)
                                break

            # We always scan ALL enabled timeframes in the background, regardless 
            # of the trade_style setting. This ensures that if the user limits new 
            # signals to Intraday, their older active Swing/BTST trades on higher 
            # timeframes continue to be monitored and managed by the scanner.
            settings_rev = settings_revision()
            ms = get_market_status()
            # Resolved once per cycle so an index-tier toggle takes effect on the
            # next scan, and every stage of this cycle sees the same set.
            scan_symbols = active_scan_symbols()
            logger.info(
                f"Starting scan: {len(scan_symbols)}/{len(SCAN_SYMBOLS)} symbols "
                f"× {len(tfs_to_scan)} timeframes [session={ms['session_status']}]"
            )
            prev_states = self._snapshot_states()
            new_results: dict[str, dict[str, SymbolResult]] = {tf: {} for tf in tfs_to_scan}
            from data_provider import prefetch_all_ohlcv
            
            # DUAL TRACKING: Fetch live option premiums for all active trades
            live_option_ltps: dict[str, float] = {}
            active_opt_keys = set()
            with self._results_lock:
                for tf_res in self._results.values():
                    for res in tf_res.values():
                        if res.latest.get("state") in ("ACTIVE", "PENDING"):
                            opt_sym = res.latest.get("option_symbol")
                            if opt_sym:
                                active_opt_keys.add(opt_sym)
            if active_opt_keys:
                try:
                    from broker_upstox import get_upstox_client
                    upstox = get_upstox_client()
                    quotes = upstox.get_quote(list(active_opt_keys))
                    for key, q in quotes.items():
                        live_option_ltps[key] = float(q.get("last_price", math.nan))
                    logger.info(f"DEBUG: active_opt_keys={active_opt_keys} quotes_returned={list(quotes.keys())} live_option_ltps={live_option_ltps}")
                except Exception as e:
                    logger.warning(f"Failed to fetch live option premiums: {e}")

            # Warm and publish one timeframe at a time. Copy-on-write result
            # dictionaries let API readers see completed symbols immediately
            # without ever iterating a dictionary that is being mutated.
            for tf in tfs_to_scan:
                prefetch_all_ohlcv(scan_symbols, [tf])
                executor = self._get_scan_executor()
                symbol_iterator = iter(scan_symbols)
                pending: dict[Any, str] = {}
                max_pending = max(2, scan_worker_count() * 2)

                def submit_next() -> bool:
                    try:
                        symbol = next(symbol_iterator)
                    except StopIteration:
                        return False
                    df = fetch_ohlcv(symbol, tf)
                    if len(df) > 400:
                        df = df.iloc[-400:].copy()
                    
                    # Find option_ltp for this symbol if active
                    opt_ltp = math.nan
                    with self._results_lock:
                        display = symbol.replace(".NS", "")
                        res = self._results.get(tf, {}).get(display)
                        if res and res.latest.get("state") in ("ACTIVE", "PENDING"):
                            opt_sym = res.latest.get("option_symbol")
                            if opt_sym:
                                opt_ltp = live_option_ltps.get(opt_sym, math.nan)

                    pending[executor.submit(_scan_preloaded, symbol, tf, df, settings_rev, opt_ltp)] = symbol
                    return True

                while len(pending) < max_pending and submit_next():
                    pass

                while pending:
                    completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in completed:
                        symbol = pending.pop(future)
                        try:
                            result = future.result()
                            if result is not None:
                                display = symbol.replace(".NS", "")
                                new_results[tf][display] = result
                                with self._results_lock:
                                    published = self._results.get(tf, {}).copy()
                                    published[display] = result
                                    self._results[tf] = published
                            else:
                                self._scan_errors += 1
                        except Exception as exc:
                            logger.error("Scan error %s/%s: %s", symbol, tf, exc)
                            self._scan_errors += 1
                        while len(pending) < max_pending and submit_next():
                            pass

                # Publish a precomputed snapshot after each complete timeframe.
                # HTTP readers never compete with process-pool result decoding.
                signal_snapshot = self._build_signals()
                trades_snapshot = self._build_active_trades()
                leaderboard_snapshot = self._build_leaderboard()
                with self._signals_lock:
                    self._signals_cache = signal_snapshot
                with self._api_cache_lock:
                    self._trades_cache = trades_snapshot
                    self._leaderboard_cache = leaderboard_snapshot

            # Final copy-on-write merge keeps the complete generation visible.
            for tf in tfs_to_scan:
                with self._results_lock:
                    merged = self._results.get(tf, {}).copy()
                    merged.update(new_results[tf])
                    self._results[tf] = merged
            with self._chart_lock:
                self._chart_cache = {
                    key: value for key, value in self._chart_cache.items()
                    if key[1] not in tfs_to_scan
                }

            # Process database state tracking (buffering & repaints)
            db = SessionLocal()
            try:
                for tf in tfs_to_scan:
                    for sym, result in new_results[tf].items():
                        self._process_db_state(db, sym, tf, result)
                # Housekeeping: signal states are per-candle scratch rows and
                # previously accumulated forever.
                cleanup_cutoff = ist_now().replace(tzinfo=None) - pd.Timedelta(days=7)
                db.query(SignalState).filter(SignalState.first_seen_time < cleanup_cutoff).delete()
                # Legacy hygiene: rows written with ".NS" symbols can never be
                # matched by the engine (it queries stripped names), so ACTIVE
                # ones would stay open forever. Close them explicitly.
                # This was the last remaining path that could write a CLOSED row
                # with pnl NULL: it set status/exit_reason/exit_time and no
                # exit_price or pnl, and it sits outside _process_db_state so it
                # bypassed close_with_mark_to_market entirely. Any row it closes
                # is now marked at its own entry (a true scratch, since these
                # rows are unmatchable and carry no usable last price) so it
                # still cannot produce a NULL.
                for zombie in db.query(Trade).filter(Trade.status == "ACTIVE", Trade.symbol.like("%.NS")).all():
                    zombie.status = "CLOSED"
                    zombie.exit_reason = "LEGACY_SYMBOL_CLEANUP [NO MARK]"
                    zombie.exit_time = ist_now().replace(tzinfo=None)
                    if zombie.exit_price is None:
                        zombie.exit_price = zombie.entry_price
                    if zombie.pnl is None:
                        zombie.pnl = 0.0
                db.commit()
            except Exception as e:
                db.rollback()
                logger.error(f"DB Error: {e}")
            finally:
                db.close()

            self._last_scan = ist_now()
            events = self._diff_states(prev_states)
            with self._events_lock:
                self._pending_events.extend(events)
                if len(self._pending_events) > 1000:
                    self._pending_events = self._pending_events[-1000:]
            self._analytics_cache.clear()
            for tenure in ("1d", "7d", "30d", "90d", "180d", "365d"):
                self.get_analytics(tenure)
            signal_snapshot = self._build_signals()
            trades_snapshot = self._build_active_trades()
            leaderboard_snapshot = self._build_leaderboard()
            with self._signals_lock:
                self._signals_cache = signal_snapshot
            with self._api_cache_lock:
                self._trades_cache = trades_snapshot
                self._leaderboard_cache = leaderboard_snapshot
            logger.info(f"Scan done. errors={self._scan_errors} events={len(self._pending_events)}")
        finally:
            scan_latency_ms = (time.monotonic() - scan_started_monotonic) * 1000.0
            with self._scan_state_lock:
                self._scan_latency_ms = scan_latency_ms
                self._scanning = False
            logger.info(
                "Scan cycle %s finished in %.1fms",
                self._scan_count,
                scan_latency_ms,
            )

    def _portfolio_gate(self, db, sym: str, tf: str) -> tuple[bool, str]:
        """Account-level entry gate, evaluated against the trades TABLE.

        This is the enforcement point the system never had. `_apply_selection`
        and `_portfolio_circuit_state` computed slots, sector caps and a daily
        loss breaker and wrote them into a `selected` flag on the /api/signals
        payload that nothing read — so `max_open_positions = 5` admitted a
        measured peak of 443 concurrent positions, and `daily_max_loss_pct = 2.0`
        required ONE symbol on ONE timeframe to lose 2% in a session because the
        counters were `run_symbol` locals rebuilt every scan.

        Counting from the DB rather than the in-memory replay makes both limits
        global across all 236 symbols and all four timeframes, and makes them
        survive a process restart for free.

        Off-switches, so this is controllable without editing code:
          max_open_positions = 0  -> unlimited positions
          max_per_sector     = 0  -> no sector cap
          daily_max_loss_pct = 0  -> no daily loss breaker

        Returns (admitted, reason). Fails OPEN on error: a risk check that throws
        must not silently halt all trading.
        """
        try:
            from settings_store import get_settings
            s = get_settings()
            max_open = int(s.get("max_open_positions", 0) or 0)
            max_sector = int(s.get("max_per_sector", 0) or 0)
            daily_max = abs(float(s.get("daily_max_loss_pct", 0) or 0))
            if max_open <= 0 and max_sector <= 0 and daily_max <= 0:
                return True, ""

            if daily_max > 0:
                today = ist_now().date()
                realized = 0.0
                for (pnl,) in (
                    db.query(Trade.pnl)
                    .filter(Trade.status == "CLOSED", Trade.exit_time >= datetime.combine(today, dt_time(0, 0)))
                    .all()
                ):
                    if pnl is not None:
                        realized += float(pnl)
                if realized <= -daily_max:
                    return False, f"daily loss breaker: realized {realized:.2f}% <= -{daily_max:.2f}%"

            if max_open <= 0 and max_sector <= 0:
                return True, ""

            open_rows = db.query(Trade).filter(Trade.status == "ACTIVE").all()
            if max_open > 0 and len(open_rows) >= max_open:
                return False, f"book full: {len(open_rows)}/{max_open} positions open"

            if max_sector > 0:
                from sectors import get_sector
                sector = get_sector(sym)
                # sectors.py maps 124 of 236 symbols to a catch-all "NSE" bucket.
                # Applying a 2-per-sector cap to that bucket would throttle 52% of
                # the universe against each other, so it is exempt until the map
                # covers every name.
                if sector and sector != "NSE":
                    in_sector = sum(1 for r in open_rows if get_sector(r.symbol) == sector)
                    if in_sector >= max_sector:
                        return False, f"sector cap: {sector} has {in_sector}/{max_sector} open"
            return True, ""
        except Exception as exc:
            logger.error("Portfolio gate failed for %s/%s (failing open): %s", sym, tf, exc)
            return True, ""

    def _process_db_state(self, db, sym, tf, result: SymbolResult):
        from datetime import datetime
        lt = result.latest
        state = str(lt.get("state", ""))
        sig = str(lt.get("signal", ""))

        # All DB times are naive IST (SQLite drops tzinfo; mixing server-local
        # datetime.now() with tz-aware bar times broke candle_timestamp lookups).
        def naive_ist_now() -> datetime:
            return ist_now().replace(tzinfo=None)

        def close_with_mark_to_market(row, reason: str, price, when) -> None:
            """Close a Trade row, ALWAYS stamping a P&L.

            294 rows previously reached status='CLOSED' with pnl NULL — 17.8% of
            all closed trades — so every win-rate and expectancy figure was
            computed on survivors only and understated the real loss. Anything
            that closes a position goes through here.
            """
            row.status = "CLOSED"
            exit_px = _safe(price)
            if exit_px is None or exit_px <= 0:
                # Falling back to entry_price fabricates a ~breakeven outcome
                # (pnl == -round_trip_cost) for a position whose real result is
                # unknown, which quietly flatters every aggregate. Mark the row
                # so these are identifiable and excludable in analytics.
                reason = f"{reason} [NO MARK]"
                logger.error(
                    "No usable exit price for %s/%s entered %s — closing at entry with an explicit marker",
                    sym, tf, row.entry_time,
                )
            row.exit_reason = reason
            row.exit_price = exit_px if (exit_px is not None and exit_px > 0) else row.entry_price
            try:
                row.exit_time = pd.Timestamp(when).to_pydatetime().replace(tzinfo=None)
            except (ValueError, TypeError, KeyError):
                row.exit_time = naive_ist_now()
            entry_px = _safe(row.entry_price)
            if entry_px and row.exit_price:
                raw = (row.exit_price - entry_px) / entry_px * 100.0
                if row.direction == "SELL":
                    raw = -raw
                # Net of the same statutory round-trip the normal exit path
                # deducts, so these rows stay comparable with the others.
                raw -= float(getattr(_CONFIGS.get(tf), "round_trip_cost_pct", 0.0) or 0.0)
                row.pnl = raw
            else:
                row.pnl = 0.0

        def find_exit_record(row):
            """Locate the replay's TradeRecord for an open DB row, by entry time."""
            for record in reversed(result.trades or []):
                try:
                    record_entry = pd.Timestamp(record.entry_time).to_pydatetime().replace(tzinfo=None)
                except (ValueError, TypeError):
                    continue
                if record_entry == row.entry_time:
                    return record
            return None

        def close_from_record(row, record) -> None:
            """Close a row from its authoritative replay TradeRecord."""
            row.status = "CLOSED"
            row.exit_reason = record.exit_reason
            row.exit_price = record.exit_price
            row.exit_time = pd.Timestamp(record.exit_time).to_pydatetime().replace(tzinfo=None)
            row.pnl = record.pnl_pct
            # Carry the terminal state too. These were written only on the
            # ACTIVE branch below, and run_symbol detects a target hit and books
            # the exit inside the SAME bar, so the snapshot carrying t1_hit=True
            # was never published as an ACTIVE row — leaving t1_hit=0 on 1636 of
            # 1657 closed rows while 77 exited with reason "T1 Booked".
            row.t1_hit, row.t2_hit, row.t3_hit = record.t1_hit, record.t2_hit, record.t3_hit
            row.profit_locked = record.profit_locked
            row.exit_confirmation_count = record.exit_confirmation_count
            if _safe(record.peak_price) is not None:
                row.peak_price = record.peak_price
            if _safe(record.trough_price) is not None:
                row.trough_price = record.trough_price

        try:
            parsed = pd.Timestamp(str(lt.get("bar_open_time")))
            parsed = parsed.tz_localize(IST_TZ) if parsed.tz is None else parsed.tz_convert(IST_TZ)
            current_ts = parsed.to_pydatetime().replace(tzinfo=None)
        except Exception:
            current_ts = naive_ist_now()

        # Resolve the ACTIVE row by entry_time, not just by (symbol, timeframe).
        # The old `.filter_by(...).first()` returned ANY open row, and the block
        # below then overwrote its direction/entry_time/entry_price/levels
        # unconditionally — so when the replay opened a NEW trade while a stale
        # ACTIVE row was still on the books, the old row was silently mutated
        # into the new trade and the previous trade's outcome was destroyed.
        replay_entry = None
        if state == "ACTIVE" and result.active_trade is not None:
            try:
                replay_entry = pd.Timestamp(result.active_trade.entry_time).to_pydatetime().replace(tzinfo=None)
            except (ValueError, TypeError):
                replay_entry = None
        try:
            active_rows = db.query(Trade).filter_by(symbol=sym, timeframe=tf, status="ACTIVE").all()
            active_db_trade = None
            if replay_entry is not None:
                active_db_trade = next((r for r in active_rows if r.entry_time == replay_entry), None)
            if active_db_trade is None:
                # No row for this specific trade. Fall back to the single open
                # row only when it cannot be a different trade.
                if replay_entry is None and active_rows:
                    active_db_trade = active_rows[0]
        except Exception as e:
            logger.error(f"Database error fetching active trade for {sym}/{tf}: {e}")
            active_rows = []
            active_db_trade = None

        # A PENDING signal is an APEX order intent, not a position.
        pending_dir = sig
        if state == "PENDING" and not pending_dir and result.pending_order is not None:
            pending_dir = "BUY" if result.pending_order.get("is_long") else "SELL"
        if state == "PENDING" and pending_dir in ("BUY", "SELL"):
            try:
                ss = db.query(SignalState).filter_by(symbol=sym, timeframe=tf, candle_timestamp=current_ts).first()
                if not ss:
                    ss = SignalState(symbol=sym, timeframe=tf, signal_dir=pending_dir, first_seen_time=naive_ist_now(), candle_timestamp=current_ts)
                    db.add(ss)
                    db.flush()

                ss.is_executed = True
            except Exception as e:
                logger.error(f"Database error managing signal state for {sym}/{tf}: {e}")

        # APEX is the sole source of truth for position lifecycle and levels.
        try:
            active = result.active_trade
            if state == "ACTIVE" and active is not None:
                direction = "BUY" if active.direction == "LONG" else "SELL"
                if active_db_trade is None:
                    # Guard: check if a CLOSED trade with the same key already
                    # exists — the scan loop can re-project an already-closed
                    # trade as new in a subsequent cycle, creating duplicates.
                    entry_dt = pd.Timestamp(active.entry_time).to_pydatetime().replace(tzinfo=None)
                    existing_closed = db.query(Trade).filter_by(
                        symbol=sym, timeframe=tf, entry_time=entry_dt, status="CLOSED"
                    ).first()
                    admitted, gate_reason = self._portfolio_gate(db, sym, tf)
                    if existing_closed is not None:
                        # This exact trade is already recorded AND closed, so its
                        # outcome is on the books; the replay is re-projecting it
                        # (repaint). Leave active_db_trade None: creating a row
                        # would duplicate it, and adopting the closed row would
                        # let the update block below overwrite a finished trade's
                        # entry price and levels.
                        logger.debug("Skipping re-projected closed trade %s/%s @ %s", sym, tf, entry_dt)
                    elif not admitted:
                        logger.info("Entry rejected for %s/%s — %s", sym, tf, gate_reason)
                    else:
                        active_db_trade = Trade(
                            symbol=sym, timeframe=tf, direction=direction,
                            entry_time=entry_dt,
                            entry_price=active.entry_price, status="ACTIVE",
                        )
                        db.add(active_db_trade)

                if active_db_trade is not None:
                    active_db_trade.direction = direction
                    active_db_trade.entry_time = pd.Timestamp(active.entry_time).to_pydatetime().replace(tzinfo=None)
                    active_db_trade.entry_price = active.entry_price
                    active_db_trade.signal_time = pd.Timestamp(active.signal_time).to_pydatetime().replace(tzinfo=None)
                    active_db_trade.entry_bar = active.entry_bar
                    active_db_trade.sl1, active_db_trade.sl2, active_db_trade.tsl = active.sl1, active.sl2, active.tsl
                    active_db_trade.tp1, active_db_trade.tp2, active_db_trade.tp3 = active.tp1, active.tp2, active.tp3
                    active_db_trade.setup, active_db_trade.stop_mode = active.setup, active.stop_mode
                    active_db_trade.option_type, active_db_trade.option_strike = active.option_type, _safe(active.option_strike)
                    active_db_trade.t1_hit, active_db_trade.t2_hit, active_db_trade.t3_hit = active.t1_hit, active.t2_hit, active.t3_hit
                    active_db_trade.profit_locked = active.profit_locked
                    active_db_trade.peak_price, active_db_trade.trough_price = active.peak_price, active.trough_price
                    active_db_trade.exit_confirmation_count = active.exit_confirmation_count
                    active_db_trade.trade_type = _trade_type(tf, active.entry_time, direction, _EXPECTED_DURATION_HRS.get(tf, 2.0))
            elif active_db_trade is not None and state == "FLAT":
                matching_exit = find_exit_record(active_db_trade)
                if matching_exit is not None:
                    close_from_record(active_db_trade, matching_exit)
                elif lt.get("exit_reason"):
                    logger.warning("Ignoring unpaired APEX exit for %s/%s; no matching APEX trade record", sym, tf)
                else:
                    # FIX DATA-04: Zombie Trades
                    # The trade started > max_input_bars ago and fell out of scanner memory.
                    # Force close it at the current price to prevent infinite lockup.
                    close_with_mark_to_market(
                        active_db_trade,
                        "System: Trade Expired (Memory Drop)",
                        lt.get("close"),
                        lt.get("timestamp"),
                    )
                    logger.warning(
                        "Force closed zombie trade for %s/%s at %s (pnl %.3f%%)",
                        sym, tf, active_db_trade.exit_price, active_db_trade.pnl,
                    )

            # Reconcile leftovers. run_symbol tracks at most ONE open trade per
            # (symbol, timeframe), so any other row still marked ACTIVE is a
            # position the replay has forgotten. Prefer its real exit record;
            # fall back to mark-to-market. Done AFTER the branches above so a
            # legitimate exit is never relabelled as an orphan.
            for stale in active_rows:
                if stale is active_db_trade or stale.status != "ACTIVE":
                    continue
                stale_exit = find_exit_record(stale)
                if stale_exit is not None:
                    close_from_record(stale, stale_exit)
                else:
                    close_with_mark_to_market(
                        stale, "System: Superseded (Orphan Reconcile)", lt.get("close"), lt.get("timestamp")
                    )
                logger.warning(
                    "Reconciled orphan ACTIVE row for %s/%s entered %s -> %s (pnl %.3f%%)",
                    sym, tf, stale.entry_time, stale.exit_reason, stale.pnl,
                )
        except Exception as e:
            logger.error(f"Database error projecting APEX state for {sym}/{tf}: {e}")

    def _fetch_and_scan(self, symbol: str, timeframe: str) -> Optional[SymbolResult]:
        from settings_store import revision as settings_revision
        df = fetch_ohlcv(symbol, timeframe)
        try:
            return _scan_preloaded(symbol, timeframe, df, settings_revision())
        except Exception as exc:
            logger.warning(f"ApexScanner failed {symbol}/{timeframe}: {exc}")
            return None

    # ── Public API ────────────────────────────────────────────────────────────

    def get_signals(
        self,
        timeframe: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> dict:
        """Return a cheap filtered view of the latest precomputed signal snapshot."""
        with self._signals_lock:
            base = self._signals_cache
        if base is None:
            base = self._build_signals()
            with self._signals_lock:
                self._signals_cache = base

        direction_upper = direction.upper() if direction else None
        signals = [
            signal for signal in base["signals"]
            if (not timeframe or timeframe not in TIMEFRAMES or signal["timeframe"] == timeframe)
            and (not direction_upper or signal["direction"] == direction_upper)
        ]
        payload = dict(base)
        payload.update({
            "signals": signals,
            "last_scan": _ts(self._last_scan),
            "scanning": self._scanning,
            "total_signals": len(signals),
            "buy_signals": sum(1 for signal in signals if signal["direction"] == "BUY"),
            "sell_signals": sum(1 for signal in signals if signal["direction"] == "SELL"),
            "active_trades": sum(1 for signal in signals if signal["state"] == "ACTIVE"),
        })
        return payload

    def _build_signals(
        self,
        timeframe: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> dict:
        signals: list[dict] = []
        tfs = [timeframe] if (timeframe and timeframe in TIMEFRAMES) else TIMEFRAMES
        dir_upper = direction.upper() if direction else None

        active_tfs_by_sym: dict[str, dict[str, str]] = {}
        with self._results_lock:
            results_snap = {t: self._results.get(t, {}).copy() for t in TIMEFRAMES}
            
        for t in TIMEFRAMES:
            if t in results_snap:
                for sym, res in results_snap[t].items():
                    lt_t = res.latest
                    state = str(lt_t.get("state", ""))
                    signal = str(lt_t.get("signal", ""))
                    if state in ("ACTIVE", "PENDING") or signal in ("BUY", "SELL"):
                        status = "NEUTRAL"
                        if state == "ACTIVE":
                            pnl = lt_t.get("live_pnl_pct")
                            if pnl is not None and not pd.isna(pnl):
                                status = "PROFIT" if pnl > 0 else ("LOSS" if pnl < 0 else "NEUTRAL")
                        if sym not in active_tfs_by_sym:
                            active_tfs_by_sym[sym] = {}
                        active_tfs_by_sym[sym][t] = status

        for tf in tfs:
            for sym, result in results_snap.get(tf, {}).items():
                lt = result.latest
                sig = str(lt.get("signal", ""))
                state = str(lt.get("state", ""))
                active_dir = str(lt.get("active_direction", ""))

                # Derive display direction
                if sig in ("BUY", "SELL"):
                    display_dir = sig
                elif state in ("ACTIVE", "PENDING"):
                    display_dir = "BUY" if active_dir == "LONG" else "SELL" if active_dir == "SHORT" else ""
                else:
                    # Dashboard is empty on weekends because the last bar is FLAT.
                    # Look back up to 72 hours for the most recent signal to display.
                    display_dir = ""
                    ms = get_market_status()
                    is_closed = not ms.get("market_open", False) or ms.get("session_status") != "OPEN"
                    if is_closed and not result.frame.empty and "signal" in result.frame.columns:
                        recent = result.frame[result.frame["signal"].isin(["BUY", "SELL"])]
                        if not recent.empty:
                            last_sig_time = pd.Timestamp(recent.index[-1])
                            now = datetime.now(IST_TZ)
                            now_naive = now.replace(tzinfo=None)
                            last_sig_naive = last_sig_time.tz_localize(None) if last_sig_time.tz is not None else last_sig_time
                            if (now_naive - last_sig_naive) < pd.Timedelta(hours=72):
                                past_sig = str(recent.iloc[-1]["signal"])
                                display_dir = past_sig
                                # Temporarily use the past signal's data for the UI
                                lt = recent.iloc[-1].to_dict()
                                sig = past_sig
                                past_sig_time = last_sig_time

                    if not display_dir:
                        continue

                if dir_upper and display_dir != dir_upper:
                    continue

                score = _signal_score_from_result(result)
                dmove = _daily_move_pct(result)

                # Trade age from signal time or active trade entry
                entry_ts = None
                if result.active_trade:
                    entry_ts = result.active_trade.entry_time or result.active_trade.signal_time
                elif result.pending_order:
                    entry_ts = result.pending_order.get("signal_time")

                age = _trade_age_hrs(entry_ts)
                eta = _eta_hrs(tf, age)

                entry_price = _first_num(lt.get("entry_price"), lt.get("close"))
                sl1 = _first_num(lt.get("sl1"), lt.get("planned_sl1"))
                sl_distance_pct = None
                if entry_price and sl1 and entry_price > 0:
                    sl_distance_pct = round(abs(entry_price - sl1) / entry_price * 100, 2)

                transition = ""
                if tf in ("15m", "1h") and state == "ACTIVE" and entry_ts is not None:
                    try:
                        now_dt = datetime.now(IST_TZ)
                        entry_dt = pd.Timestamp(entry_ts)
                        entry_dt = entry_dt.tz_localize(IST_TZ) if entry_dt.tz is None else entry_dt.tz_convert(IST_TZ)
                        # From 15:15 an intraday position becomes a carry decision:
                        # BTST for longs; cash shorts CANNOT be carried overnight.
                        if entry_dt.date() == now_dt.date() and (now_dt.hour, now_dt.minute) >= (15, 15):
                            transition = "BTST" if display_dir == "BUY" else "MUST_EXIT"
                    except Exception:
                        pass

                signals.append({
                    "symbol": sym,
                    "sector": get_sector(sym),
                    "timeframe": tf,
                    "direction": display_dir,
                    "score": round(score, 1),
                    "state": state,
                    "entry": _tick(entry_price),
                    "entry_price": _tick(entry_price),
                    "sl1": _tick(sl1),
                    "sl2": _tick(_safe(lt.get("sl2"))),
                    "tp1": _tick(_first_num(lt.get("tp1"), lt.get("planned_tp1"))),
                    "tp2": _tick(_first_num(lt.get("tp2"), lt.get("planned_tp2"))),
                    "tp3": _tick(_first_num(lt.get("tp3"), lt.get("planned_tp3"))),
                    "setup": str(lt.get("setup", "")),
                    "timestamp": _ts(lt.get("timestamp")),
                    "signal_time": _ts(entry_ts) if entry_ts is not None else _ts(lt.get("timestamp")),
                    "close": _safe(lt.get("close")) or 0.0,
                    "rsi": _safe(lt.get("rsi")),
                    "adx": _safe(lt.get("adx")),
                    "bias": str(lt.get("bias", "")),
                    "bull_score": _safe(lt.get("bull_score")),
                    "bear_score": _safe(lt.get("bear_score")),
                    "win_rate_pct": _safe(lt.get("win_rate_pct")),
                    "pnl_pct": _safe(lt.get("total_pnl_pct")),
                    "live_pnl_pct": _safe(lt.get("live_pnl_pct")),
                    "live_pnl_abs": _safe(lt.get("live_pnl_abs")),
                    "live_rr": _safe(lt.get("live_rr")),
                    "regime_15m": str(lt.get("regime_15m", "")),
                    "regime_1h": str(lt.get("regime_1h", "")),
                    "regime_4h": str(lt.get("regime_4h", "")),
                    "regime_1d": str(lt.get("regime_1d", "")),
                    "intraday_or_swing": _trade_type(tf, entry_ts or lt.get("timestamp"), display_dir, _EXPECTED_DURATION_HRS[tf]),
                    "daily_move_pct": dmove,
                    "trade_age_hrs": age,
                    "eta_hrs": eta,
                    "expected_duration_hrs": _EXPECTED_DURATION_HRS[tf],
                    "active_timeframes": active_tfs_by_sym.get(sym, {}),
                    "relative_volume": _safe(lt.get("relative_volume")),
                    "sl_distance_pct": sl_distance_pct,
                    "transition": transition,
                    "option_type": str(lt.get("option_type", "")),
                    "option_strike": _safe(lt.get("option_strike")),
                    "option_symbol": str(lt.get("option_symbol", "")),
                    "option_entry": _safe(lt.get("option_entry")),
                    "option_ltp": _safe(lt.get("option_ltp")),
                    "option_sl1": _safe(lt.get("option_sl1")),
                    "option_sl2": _safe(lt.get("option_sl2")),
                    "option_tsl": _safe(lt.get("option_tsl")),
                    "option_tp1": _safe(lt.get("option_tp1")),
                    "option_tp2": _safe(lt.get("option_tp2")),
                    "option_tp3": _safe(lt.get("option_tp3")),
                })

        # Newest signal first, then highest score within identical timestamps.
        signals.sort(key=lambda s: (s.get("signal_time") or "", s.get("score") or 0.0), reverse=True)

        # Top-N selection + portfolio circuit breaker: a manual trader takes only
        # the few best setups and stands down after losses. Surface a `selected`
        # flag so the automated set matches that discipline instead of firing on
        # every passing signal across 236 names.
        selection = self._apply_selection(signals)

        buy_n  = sum(1 for s in signals if s["direction"] == "BUY")
        sell_n = sum(1 for s in signals if s["direction"] == "SELL")
        active_n = sum(1 for s in signals if s["state"] == "ACTIVE")

        market_breadth = self._market_breadth()

        return {
            "signals": signals,
            "last_scan": _ts(self._last_scan),
            "scanning": self._scanning,
            "total_signals": len(signals),
            "buy_signals": buy_n,
            "sell_signals": sell_n,
            "active_trades": active_n,
            "market_breadth": market_breadth,
            "selection": selection,
        }

    def _portfolio_circuit_state(self) -> dict:
        """Account-level throttle derived from ACTIVE-trade drawdown and today's
        realized exits. When tripped, no NEW entries are selected — the automated
        analogue of a human standing down after a bad run.

        Note: trades are engine-simulated on a rolling window, so this is an
        approximation, not a broker-verified account state.
        """
        from settings_store import get_settings
        s = get_settings()
        daily_max = abs(float(s.get("daily_max_loss_pct", 3.0)))
        max_open = int(s.get("max_open_positions", 5))
        today = ist_now().date()

        open_drawdown = 0.0
        active_count = 0
        realized_today = 0.0
        with self._results_lock:
            results_snap = {t: self._results.get(t, {}).copy() for t in TIMEFRAMES}
            
        for tf in TIMEFRAMES:
            for _sym, res in results_snap.get(tf, {}).items():
                lt = res.latest
                if str(lt.get("state", "")) == "ACTIVE":
                    active_count += 1
                    pnl = _safe(lt.get("live_pnl_pct"))
                    if pnl is not None:
                        open_drawdown += pnl
                for tr in res.trades:
                    try:
                        if tr.exit_time and pd.Timestamp(tr.exit_time).date() == today:
                            realized_today += float(tr.pnl_pct)
                    except Exception:
                        pass

        # Convert stock-scale pnl sums to portfolio-scale (assuming equal sizing 1/N)
        port_realized = realized_today / max_open if max_open > 0 else realized_today
        port_open = open_drawdown / max_open if max_open > 0 else open_drawdown

        reasons = []
        if port_realized <= -daily_max:
            reasons.append(f"today's realized loss {port_realized:.2f}% <= -{daily_max:.2f}%")
        if port_open <= -daily_max:
            reasons.append(f"open drawdown {port_open:.2f}% <= -{daily_max:.2f}%")
        book_full = active_count >= max_open
        return {
            "paused": bool(reasons),
            "book_full": book_full,
            "active_count": active_count,
            "max_open_positions": max_open,
            "open_drawdown_pct": round(port_open, 2),
            "realized_today_pct": round(port_realized, 2),
            "reasons": reasons,
        }

    def _apply_selection(self, signals: list[dict]) -> dict:
        """Tag the top-ranked NEW entry candidates as `selected`, honoring
        max_open_positions (minus the current book) and max_per_sector. Existing
        ACTIVE trades are never de-selected; only fresh entries are gated."""
        from settings_store import get_settings
        s = get_settings()
        max_open = int(s.get("max_open_positions", 5))
        max_per_sector = int(s.get("max_per_sector", 2))
        circuit = self._portfolio_circuit_state()

        for sig in signals:
            sig["selected"] = False
            sig["selection_rank"] = None

        # New entry candidates = fresh signals / pending orders (not already ACTIVE).
        candidates = [s for s in signals if s.get("state") != "ACTIVE"]
        candidates.sort(key=lambda s: (s.get("score") or 0.0), reverse=True)

        slots = max(0, max_open - circuit["active_count"])
        selected = 0
        # RX-03 FIX: Seed per_sector with existing ACTIVE trades so that the
        # sector cap is enforced against the *full* portfolio, not just new
        # candidates. Without this, 2 active BANK trades + a new BANK candidate
        # would pass the cap because per_sector started at 0.
        per_sector: dict[str, int] = {}
        for sig in signals:
            if sig.get("state") == "ACTIVE":
                sec = str(sig.get("sector", ""))
                per_sector[sec] = per_sector.get(sec, 0) + 1
        if not circuit["paused"]:
            for sig in candidates:
                if selected >= slots:
                    break
                sec = str(sig.get("sector", ""))
                # Skip sector cap for the generic NSE bucket so it doesn't starve 52% of the market
                if sec and sec != "NSE" and per_sector.get(sec, 0) >= max_per_sector:
                    continue
                sig["selected"] = True
                selected += 1
                sig["selection_rank"] = selected
                per_sector[sec] = per_sector.get(sec, 0) + 1

        return {
            "circuit_breaker": circuit,
            "max_open_positions": max_open,
            "max_per_sector": max_per_sector,
            "open_slots": slots,
            "selected_count": selected,
            "candidate_count": len(candidates),
        }

    def _market_breadth(self) -> dict:
        """Bullish/bearish share of the universe from the daily-timeframe bias.

        The engine emits "STR BULL"/"MILD BULL"/"STR BEAR"/"MILD BEAR"/"NEUTRAL";
        the old exact comparison against "BULLISH"/"BEARISH" never matched.
        """
        breadth_buy = breadth_sell = 0
        with self._results_lock:
            daily_snap = self._results.get("1d", {}).copy()
            
        for _sym, result in daily_snap.items():
            bias = str(result.latest.get("bias", ""))
            if "BULL" in bias:
                breadth_buy += 1
            elif "BEAR" in bias:
                breadth_sell += 1
        total = breadth_buy + breadth_sell
        return {
            "bullish_pct": round(breadth_buy / total * 100) if total > 0 else 50,
            "bearish_pct": round(breadth_sell / total * 100) if total > 0 else 50,
            "bullish_count": breadth_buy,
            "bearish_count": breadth_sell,
            "sample": total,
        }

    def get_active_trades(self) -> dict:
        """Return the active-trades snapshot assembled by the scan worker."""
        with self._api_cache_lock:
            cached = self._trades_cache
        if cached is None:
            cached = self._build_active_trades()
            with self._api_cache_lock:
                self._trades_cache = cached
        return cached

    def _build_active_trades(self) -> dict:
        trades: list[dict] = []

        with self._results_lock:
            results_snap = {t: self._results.get(t, {}).copy() for t in TIMEFRAMES}

        for tf in TIMEFRAMES:
            for sym, result in results_snap[tf].items():
                lt = result.latest
                state = str(lt.get("state", ""))
                if state not in ("ACTIVE", "PENDING"):
                    continue

                active = result.active_trade
                direction = str(lt.get("active_direction", ""))
                dmove = _daily_move_pct(result)

                if active is not None:
                    entry = _safe(active.entry_price) or 0.0
                    current = _safe(lt.get("close")) or entry
                    sign = 1 if direction == "LONG" else -1
                    pnl_pts = (current - entry) * sign
                    pnl_pct = (pnl_pts / entry * 100) if entry else 0.0

                    # Trailing stop
                    tsl_val = _safe(active.tsl)

                    # Trade age and ETA
                    entry_ts = active.entry_time or active.signal_time
                    age = _trade_age_hrs(entry_ts)
                    eta = _eta_hrs(tf, age)

                    # Live move (points and %)
                    live_pts = pnl_pts
                    live_pct = round(pnl_pct, 2)

                    trades.append({
                        "symbol": sym,
                        "timeframe": tf,
                        "direction": "BUY" if direction == "LONG" else "SELL",
                        "entry_price": round(entry, 2),
                        "current_price": round(current, 2),
                        "sl1": _tick(_safe(active.sl1)) or 0.0,
                        "sl2": _tick(_safe(active.sl2)),
                        "tp1": _tick(_safe(active.tp1)) or 0.0,
                        "tp2": _tick(_safe(active.tp2)),
                        "tp3": _tick(_safe(active.tp3)),
                        "pnl_pct": round(pnl_pct, 2),
                        "pnl_points": round(pnl_pts, 2),
                        "t1_hit": bool(active.t1_hit),
                        "t2_hit": bool(active.t2_hit),
                        "t3_hit": bool(active.t3_hit),
                        "setup": str(active.setup),
                        "entry_time": _ts(active.entry_time),
                        "signal_time": _ts(active.signal_time),
                        "state": state,
                        "live_rr": _safe(lt.get("live_rr")),
                        "tsl": tsl_val,
                        # ── New professional fields ──
                        "intraday_or_swing": _trade_type(tf, active.entry_time or active.signal_time, direction, _EXPECTED_DURATION_HRS[tf]),
                        "daily_move_pct": dmove,
                        "live_move_pct": live_pct,
                        "live_move_pts": round(live_pts, 2),
                        "trade_age_hrs": age,
                        "eta_hrs": eta,
                        "expected_duration_hrs": _EXPECTED_DURATION_HRS[tf],
                        "score": _signal_score_from_result(result),
                        "regime_15m": str(lt.get("regime_15m", "")),
                        "regime_1h": str(lt.get("regime_1h", "")),
                        "regime_4h": str(lt.get("regime_4h", "")),
                        "regime_1d": str(lt.get("regime_1d", "")),
                        "option_type": str(lt.get("option_type", "")),
                        "option_strike": _safe(lt.get("option_strike")),
                        "option_symbol": str(lt.get("option_symbol", "")),
                        "option_entry": _safe(lt.get("option_entry")),
                        "option_ltp": _safe(lt.get("option_ltp")),
                        "option_sl1": _safe(lt.get("option_sl1")),
                        "option_tsl": _safe(lt.get("option_tsl")),
                        "option_tp1": _safe(lt.get("option_tp1")),
                    })
                elif result.pending_order:
                    po = result.pending_order
                    is_long = bool(po.get("is_long"))
                    po_sig_time = po.get("signal_time")
                    age = _trade_age_hrs(po_sig_time)
                    eta = _eta_hrs(tf, age)

                    trades.append({
                        "symbol": sym,
                        "timeframe": tf,
                        "direction": "BUY" if is_long else "SELL",
                        "entry_price": _safe(po.get("close")) or 0.0,
                        "current_price": _safe(lt.get("close")) or 0.0,
                        "sl1": _safe(po.get("planned_sl1")) or 0.0,
                        "sl2": _safe(po.get("planned_sl2")),
                        "tp1": _safe(po.get("planned_tp1")) or 0.0,
                        "tp2": _safe(po.get("planned_tp2")),
                        "tp3": _safe(po.get("planned_tp3")),
                        "pnl_pct": 0.0,
                        "pnl_points": 0.0,
                        "t1_hit": False,
                        "t2_hit": False,
                        "t3_hit": False,
                        "setup": str(po.get("setup", "")),
                        "entry_time": _ts(po_sig_time),
                        "signal_time": _ts(po_sig_time),
                        "state": state,
                        "live_rr": None,
                        "tsl": None,
                        # ── New professional fields ──
                        "intraday_or_swing": _trade_type(tf, po_sig_time, "LONG" if is_long else "SHORT", _EXPECTED_DURATION_HRS[tf]),
                        "daily_move_pct": dmove,
                        "live_move_pct": 0.0,
                        "live_move_pts": 0.0,
                        "trade_age_hrs": age,
                        "eta_hrs": eta,
                        "expected_duration_hrs": _EXPECTED_DURATION_HRS[tf],
                        "score": _signal_score_from_result(result),
                    })

        return {
            "trades": trades,
            "total": len(trades),
            "long_count":  sum(1 for t in trades if t["direction"] == "BUY"),
            "short_count": sum(1 for t in trades if t["direction"] == "SELL"),
        }

    def get_leaderboard(self, timeframe: Optional[str] = None) -> dict:
        """Return a filtered view of the precomputed leaderboard snapshot."""
        with self._api_cache_lock:
            cached = self._leaderboard_cache
        if cached is None:
            cached = self._build_leaderboard()
            with self._api_cache_lock:
                self._leaderboard_cache = cached

        rows = cached["rows"]
        if timeframe and timeframe in TIMEFRAMES:
            rows = [row for row in rows if row["timeframe"] == timeframe]
        return {
            "rows": rows,
            "last_scan": _ts(self._last_scan),
            "scanning": self._scanning,
        }

    def _build_leaderboard(self, timeframe: Optional[str] = None) -> dict:
        rows: list[dict] = []
        tfs = [timeframe] if (timeframe and timeframe in TIMEFRAMES) else TIMEFRAMES

        with self._results_lock:
            results_snap = {t: self._results.get(t, {}).copy() for t in tfs}

        for tf in tfs:
            for sym, result in results_snap[tf].items():
                lt = result.latest
                score = _signal_score_from_result(result)
                board_dir = str(lt.get("signal", ""))
                if not board_dir:
                    active_dir = str(lt.get("active_direction", ""))
                    board_dir = "BUY" if active_dir == "LONG" else "SELL" if active_dir == "SHORT" else ""
                rows.append({
                    "symbol": sym,
                    "sector": get_sector(sym),
                    "relative_volume": _safe(lt.get("relative_volume")),
                    "timeframe": tf,
                    "state": str(lt.get("state", "FLAT")),
                    "signal": str(lt.get("signal", "")),
                    "score": round(score, 1),
                    "close": _safe(lt.get("close")) or 0.0,
                    "bias": str(lt.get("bias", "")),
                    "rsi": _safe(lt.get("rsi")),
                    "adx": _safe(lt.get("adx")),
                    "win_rate_pct": _safe(lt.get("win_rate_pct")),
                    "pnl_pct": _safe(lt.get("total_pnl_pct")),
                    "live_pnl_pct": _safe(lt.get("live_pnl_pct")),
                    "live_pnl_abs": _safe(lt.get("live_pnl_abs")),
                    "setup": str(lt.get("setup", "")),
                    "regime_15m": str(lt.get("regime_15m", "")),
                    "regime_1h": str(lt.get("regime_1h", "")),
                    "regime_4h": str(lt.get("regime_4h", "")),
                    "regime_1d": str(lt.get("regime_1d", "")),
                    "option_type": str(lt.get("option_type", "")),
                    "option_strike": _safe(lt.get("option_strike")),
                    "option_symbol": str(lt.get("option_symbol", "")),
                    "option_entry": _safe(lt.get("option_entry")),
                    "option_ltp": _safe(lt.get("option_ltp")),
                    "intraday_or_swing": _trade_type(tf, lt.get("timestamp"), board_dir, _EXPECTED_DURATION_HRS[tf]),
                    "daily_move_pct": _daily_move_pct(result),
                })

        _STATE_RANK = {"ACTIVE": 3, "PENDING": 2, "FLAT": 0}
        _SIG_RANK   = {"BUY": 1, "SELL": 1}
        rows.sort(key=lambda r: (
            -_STATE_RANK.get(r["state"], 0),
            -_SIG_RANK.get(r["signal"], 0),
            -(r["score"] or 0),
        ))

        return {
            "rows": rows,
            "last_scan": _ts(self._last_scan),
            "scanning": self._scanning,
        }

    def get_analytics(self, tenure: Optional[str] = "30d") -> dict:
        """Aggregate comprehensive real data across all timeframes, symbols, and trade records with tenure capping."""
        tenure_map = {
            "1d": 1,
            "7d": 7,
            "30d": 30,
            "90d": 90,
            "180d": 180,
            "365d": 365,
        }
        raw_tenure = str(tenure or "30d").lower().strip()
        days_limit = tenure_map.get(raw_tenure, 30)
        days_limit = min(days_limit, 365)  # Hard cap at 365 days maximum limit

        cache_key = (days_limit, self._scan_count)
        if cache_key in self._analytics_cache:
            return self._analytics_cache[cache_key]

        now = ist_now()
        cutoff_dt = now - pd.Timedelta(days=days_limit)

        timeframe_stats: dict[str, dict] = {}
        all_trades_by_tf: dict[str, list[dict]] = {tf: [] for tf in TIMEFRAMES}
        all_trades_flat: list[dict] = []

        total_active = 0
        total_signals = 0

        with self._results_lock:
            results_snap = {t: self._results.get(t, {}).copy() for t in TIMEFRAMES}

        for tf in TIMEFRAMES:
            for sym, result in results_snap.get(tf, {}).items():
                lt = result.latest
                state = str(lt.get("state", ""))
                sig = str(lt.get("signal", ""))
                if state in ("ACTIVE", "PENDING"):
                    total_active += 1
                if sig in ("BUY", "SELL"):
                    total_signals += 1

                for tr in result.trades:
                    check_ts = tr.exit_time if (tr.exit_time and tr.exit_time != "—") else tr.entry_time
                    if check_ts and check_ts != "—":
                        try:
                            dt = pd.to_datetime(check_ts)
                            if dt.tz is None:
                                dt = dt.tz_localize("Asia/Kolkata")
                            if dt < cutoff_dt:
                                continue  # Exclude trades older than days_limit (and capped at max 365d)
                        except Exception:
                            pass
                    t_dict = {
                        "symbol": sym,
                        "timeframe": tf,
                        "sector": get_sector(sym),
                        "entry_time": _ts(tr.entry_time),
                        "exit_time": _ts(tr.exit_time),
                        "direction": tr.direction,
                        "entry_price": tr.entry_price,
                        "exit_price": tr.exit_price,
                        "pnl_pct": round(tr.pnl_pct, 2),
                        "pnl_r": round(tr.pnl_r, 2),
                        "signal_score": round(float(getattr(tr, "signal_score", 0.0) or 0.0), 1),
                        "option_pnl_pct": round(float(getattr(tr, "option_pnl_pct", 0.0) or 0.0), 2),
                        "setup": tr.setup or lt.get("setup", ""),
                        "exit_reason": tr.exit_reason,
                        "bars_held": tr.bars_held,
                        "intraday_or_swing": _trade_type(tf, tr.entry_time, tr.direction, _EXPECTED_DURATION_HRS[tf], exit_ts=tr.exit_time),
                    }
                    all_trades_by_tf[tf].append(t_dict)
                    all_trades_flat.append(t_dict)

            # HONESTY RULE (audit N1): metrics come from real simulated trade
            # records or they are 0.0 with insufficient_data=True. No metric in
            # this endpoint may be synthesized from constants or win-rate math.
            tf_trades = all_trades_by_tf[tf]
            tf_closed_count = len(tf_trades)
            tf_wins = sum(1 for t in tf_trades if t["pnl_pct"] > 0)
            tf_losses = sum(1 for t in tf_trades if t["pnl_pct"] < 0)
            tf_gross_profit = sum(t["pnl_pct"] for t in tf_trades if t["pnl_pct"] > 0)
            tf_gross_loss = abs(sum(t["pnl_pct"] for t in tf_trades if t["pnl_pct"] < 0))
            tf_pnls = [t["pnl_pct"] for t in tf_trades]

            win_rate = round(tf_wins / tf_closed_count * 100, 1) if tf_closed_count > 0 else 0.0
            profit_factor = round(tf_gross_profit / tf_gross_loss, 2) if tf_gross_loss > 1e-6 else 0.0

            # Per-trade quality ratio (mean/std of trade PnL%). The previous
            # value was annualized with sqrt(252) as if trades were daily
            # returns, and fabricated entirely below ~2 trades.
            if len(tf_pnls) >= 5 and np.std(tf_pnls, ddof=1) > 1e-6:
                sharpe = round(float(np.mean(tf_pnls) / np.std(tf_pnls, ddof=1)), 2)
            else:
                sharpe = 0.0

            total_pnl = round(sum(tf_pnls), 2)
            avg_win = round(tf_gross_profit / tf_wins, 2) if tf_wins > 0 else 0.0
            avg_loss = round(tf_gross_loss / tf_losses, 2) if tf_losses > 0 else 0.0
            payoff = round(avg_win / avg_loss, 2) if avg_loss > 0 else 0.0
            if tf_closed_count >= 5 and avg_loss > 0 and payoff > 0:
                w = win_rate / 100.0
                kelly = w - (1.0 - w) / payoff
                avg_kelly = round(max(0.0, min(kelly * 50.0, 30.0)), 1)
            else:
                avg_kelly = 0.0

            timeframe_stats[tf] = {
                "timeframe": tf,
                "num_trades": tf_closed_count,
                "wins": tf_wins,
                "losses": tf_losses,
                "win_rate_pct": win_rate,
                "profit_factor": profit_factor,
                "sharpe_ratio": sharpe,
                "total_pnl_pct": total_pnl,
                "avg_win_pct": avg_win,
                "avg_loss_pct": avg_loss,
                "payoff_ratio": payoff,
                "half_kelly_pct": avg_kelly,
                "insufficient_data": tf_closed_count < 5,
            }

        all_closed = sum(timeframe_stats[tf]["num_trades"] for tf in TIMEFRAMES)
        all_wins = sum(timeframe_stats[tf]["wins"] for tf in TIMEFRAMES)
        all_losses = sum(timeframe_stats[tf]["losses"] for tf in TIMEFRAMES)
        all_win_rate = round(all_wins / all_closed * 100, 1) if all_closed > 0 else 0.0
        all_total_pnl = round(sum(timeframe_stats[tf]["total_pnl_pct"] for tf in TIMEFRAMES), 2)
        # Option-leg (CE/PE) performance estimate — the instrument actually
        # traded — vs the cash number above. Theta/spread make this lower.
        opt_pnls = [float(t.get("option_pnl_pct", 0.0)) for t in all_trades_flat]
        opt_wins = sum(1 for v in opt_pnls if v > 0)
        option_win_rate = round(opt_wins / len(opt_pnls) * 100, 1) if opt_pnls else 0.0
        option_total_pnl = round(sum(opt_pnls), 2)
        
        # Pooled metrics for ALL row
        all_pnls = [t["pnl_pct"] for t in all_trades_flat]
        if len(all_pnls) >= 5 and np.std(all_pnls, ddof=1) > 1e-6:
            all_sharpe = round(float(np.mean(all_pnls) / np.std(all_pnls, ddof=1)), 2)
        else:
            all_sharpe = 0.0
            
        all_gp = sum(pnl for pnl in all_pnls if pnl > 0)
        all_gl = abs(sum(pnl for pnl in all_pnls if pnl < 0))
        all_pf = round(all_gp / all_gl, 2) if all_gl > 1e-6 else 0.0
        all_avg_win = round(all_gp / all_wins, 2) if all_wins > 0 else 0.0
        all_avg_loss = round(all_gl / all_losses, 2) if all_losses > 0 else 0.0
        all_payoff = round(all_avg_win / all_avg_loss, 2) if all_avg_loss > 0 else 0.0
        hk = round((all_win_rate / 100.0) - (1.0 - (all_win_rate / 100.0)) / all_payoff, 3) * 0.5 * 100 if all_payoff > 0 else 0.0
        all_hk = round(max(0.0, hk), 1)

        timeframe_stats["ALL"] = {
            "timeframe": "ALL",
            "num_trades": all_closed,
            "wins": all_wins,
            "losses": all_losses,
            "win_rate_pct": all_win_rate,
            "profit_factor": all_pf,
            "sharpe_ratio": all_sharpe,
            "total_pnl_pct": all_total_pnl,
            "avg_win_pct": all_avg_win,
            "avg_loss_pct": all_avg_loss,
            "payoff_ratio": all_payoff,
            "half_kelly_pct": all_hk,
            "insufficient_data": all_closed < 5,
        }

        # Strategy Type Breakdown (Intraday vs Swing vs BTST/STBT).
        # _trade_type returns title-case values; the old comparison against
        # "INTRADAY" and the setup-string BTST search never matched anything.
        categories = {"INTRADAY": [], "SWING": [], "BTST_STBT": []}
        for tr in all_trades_flat:
            trade_type = str(tr.get("intraday_or_swing", "")).upper()
            tf = tr["timeframe"]
            if trade_type in ("BTST", "STBT"):
                categories["BTST_STBT"].append(tr)
            elif trade_type == "INTRADAY":
                categories["INTRADAY"].append(tr)
            else:
                categories["SWING"].append(tr)

        strategy_breakdown = {}
        for cat_name, tr_list in categories.items():
            cnt = len(tr_list)
            wins = sum(1 for t in tr_list if t["pnl_pct"] > 0)
            losses = sum(1 for t in tr_list if t["pnl_pct"] < 0)
            wr = round(wins / cnt * 100, 1) if cnt > 0 else 0.0
            tot_pnl = round(sum(t["pnl_pct"] for t in tr_list), 2)
            gp = sum(t["pnl_pct"] for t in tr_list if t["pnl_pct"] > 0)
            gl = abs(sum(t["pnl_pct"] for t in tr_list if t["pnl_pct"] < 0))
            pf = round(gp / gl, 2) if gl > 1e-6 else 0.0

            pnls = [t["pnl_pct"] for t in tr_list]
            if len(pnls) >= 5 and np.std(pnls, ddof=1) > 1e-6:
                sh = round(float(np.mean(pnls) / np.std(pnls, ddof=1)), 2)
            else:
                sh = 0.0

            strategy_breakdown[cat_name] = {
                "category": cat_name,
                "num_trades": cnt,
                "wins": wins,
                "losses": losses,
                "win_rate_pct": wr,
                "total_pnl_pct": tot_pnl,
                "profit_factor": pf,
                "sharpe_ratio": sh,
                "avg_trade_pnl_pct": round(tot_pnl / cnt, 2) if cnt > 0 else 0.0,
            }

        daily_pnl = 0.0
        weekly_pnl = 0.0
        monthly_pnl = 0.0
        daily_trades = 0
        weekly_trades = 0
        monthly_trades = 0

        live_active_pnl_pct = 0.0
        live_active_pnl_abs = 0.0
        for tf in TIMEFRAMES:
            for sym, res in self._results.get(tf, {}).items():
                lt = res.latest
                if lt.get("state") in ("ACTIVE", "PENDING"):
                    live_active_pnl_pct += _safe(lt.get("live_pnl_pct")) or 0.0
                    live_active_pnl_abs += _safe(lt.get("live_pnl_abs")) or 0.0

        for tr in all_trades_flat:
            ext = tr.get("exit_time")
            if ext and ext != "—":
                try:
                    dt = pd.to_datetime(ext)
                    if dt.tz is None:
                        dt = dt.tz_localize("Asia/Kolkata")
                    days_ago = (now - dt).total_seconds() / 86400.0
                    pnl = tr["pnl_pct"]
                    if days_ago <= 1.0:
                        daily_pnl += pnl
                        daily_trades += 1
                    if days_ago <= 7.0:
                        weekly_pnl += pnl
                        weekly_trades += 1
                    if days_ago <= 30.0:
                        monthly_pnl += pnl
                        monthly_trades += 1
                except Exception:
                    pass

        # daily_pnl += round(live_active_pnl_pct, 2)
        # weekly_pnl += round(live_active_pnl_pct, 2)
        # monthly_pnl += round(live_active_pnl_pct, 2)

        # live_abs_inr is the SAME live snapshot for every window (it is a
        # per-share points sum, not INR — there is no position sizing yet).
        # The old x2.4 / x6.8 weekly/monthly multipliers were pure fabrication.
        live_points = round(live_active_pnl_abs, 2)
        period_breakdown = {
            "daily": {
                "period": "Today (Daily)",
                "pnl_pct": round(daily_pnl, 2),
                "live_abs_inr": live_points,
                "trades_closed": daily_trades,
            },
            "weekly": {
                "period": "Last 7 Days (Weekly)",
                "pnl_pct": round(weekly_pnl, 2),
                "live_abs_inr": live_points,
                "trades_closed": weekly_trades,
            },
            "monthly": {
                "period": "Last 30 Days (Monthly)",
                "pnl_pct": round(monthly_pnl, 2),
                "live_abs_inr": live_points,
                "trades_closed": monthly_trades,
            },
        }

        equity_curve = []
        cumulative = 100.0
        day_pnls: dict[str, float] = {}
        for tr in all_trades_flat:
            ext = tr.get("exit_time")
            if ext and ext != "—":
                try:
                    dt_str = ext[:10]
                    day_pnls[dt_str] = day_pnls.get(dt_str, 0.0) + tr["pnl_pct"]
                except Exception:
                    pass
                    
        # Add live PnL to today's equity curve
        today_str = now.strftime("%Y-%m-%d")
        for tf in TIMEFRAMES:
            for sym, res in self._results.get(tf, {}).items():
                lt = res.latest
                if lt.get("state") in ("ACTIVE", "PENDING"):
                    pnl_pct = _safe(lt.get("live_pnl_pct")) or 0.0
                    day_pnls[today_str] = day_pnls.get(today_str, 0.0) + pnl_pct

        # Plot exact daily points for the chosen tenure (max 365 days limit)
        plot_days = min(days_limit, 365)
        for idx in range(plot_days, -1, -1):
            d = (now - pd.Timedelta(days=idx)).strftime("%Y-%m-%d")
            # Realized trades only — unrealized live PnL no longer inflates
            # today's point (it is reported separately in period_breakdown).
            pnl_day = round(day_pnls.get(d, 0.0), 2)
            cumulative = round(cumulative + pnl_day, 2)
            equity_curve.append({
                "date": d,
                "daily_pnl_pct": pnl_day,
                "equity_index": cumulative,
            })

        sector_map: dict[str, list[dict]] = {}
        for tr in all_trades_flat:
            sec = tr.get("sector") or get_sector(tr.get("symbol", "")) or "Others"
            if sec not in sector_map:
                sector_map[sec] = []
            sector_map[sec].append(tr)

        sectors_list = []
        for sec, tlist in sector_map.items():
            s_cnt = len(tlist)
            s_wins = sum(1 for t in tlist if t["pnl_pct"] > 0)
            s_wr = round(s_wins / s_cnt * 100, 1) if s_cnt > 0 else 0.0
            s_pnl = round(sum(t["pnl_pct"] for t in tlist), 2)
            s_pnls = [t["pnl_pct"] for t in tlist]
            if len(s_pnls) >= 5 and np.std(s_pnls, ddof=1) > 1e-6:
                s_sh = round(float(np.mean(s_pnls) / np.std(s_pnls, ddof=1)), 2)
            else:
                s_sh = 0.0
            sectors_list.append({
                "sector": sec,
                "trades_count": s_cnt,
                "win_rate_pct": s_wr,
                "total_pnl_pct": s_pnl,
                "sharpe_ratio": s_sh,
            })
        sectors_list.sort(key=lambda x: -x["total_pnl_pct"])

        # Win rate by signal-score band. The blended overall number pools every
        # signal; a manual trader takes only the top setups, so this table lets
        # the two be compared like-for-like (see the audit's DIV-01/DIV-06).
        score_bands = [(0, 60), (60, 70), (70, 80), (80, 90), (90, 100.01)]
        score_band_breakdown = []
        for lo, hi in score_bands:
            band = [t for t in all_trades_flat if lo <= float(t.get("signal_score", 0.0)) < hi]
            b_cnt = len(band)
            b_wins = sum(1 for t in band if t["pnl_pct"] > 0)
            score_band_breakdown.append({
                "score_band": f"{lo:.0f}-{min(hi,100):.0f}",
                "trades_count": b_cnt,
                "win_rate_pct": round(b_wins / b_cnt * 100, 1) if b_cnt > 0 else 0.0,
                "total_pnl_pct": round(sum(t["pnl_pct"] for t in band), 2),
            })

        payload = {
            "timeframe_breakdown": timeframe_stats,
            "strategy_breakdown": strategy_breakdown,
            "period_breakdown": period_breakdown,
            "equity_curve": equity_curve,
            "sector_performance": sectors_list,
            "score_band_breakdown": score_band_breakdown,
            "summary": {
                "total_symbols": len(SCAN_SYMBOLS),
                "total_active_trades": total_active,
                "total_signals": total_signals,
                "overall_win_rate_pct": all_win_rate,
                "overall_profit_factor": all_pf,
                "overall_sharpe_ratio": all_sharpe,
                "total_historical_trades": all_closed,
                # Estimated performance of the ATM option leg actually traded.
                "option_win_rate_pct": option_win_rate,
                "option_total_pnl_pct": option_total_pnl,
                "selected_tenure": raw_tenure.upper(),
                "max_tenure_limit": "365D",
                "last_scan": _ts(self._last_scan),
                # Provenance so nobody mistakes these for broker-verified fills:
                # trades are engine-simulated on a rolling candle window.
                "data_basis": "SIMULATED_ROLLING_WINDOW",
                # P&L is measured on the CASH underlying, NET of the configured
                # round-trip cost. The option (CE/PE) leg actually traded will
                # under-perform this due to theta/spread — do not read these as
                # option returns.
                "pnl_basis": "CASH_UNDERLYING_NET_OF_COST",
            }
        }
        self._analytics_cache[cache_key] = payload
        return payload

    @staticmethod
    def _canonical_symbol(symbol: str) -> str:
        return symbol.upper().replace(".NS", "")

    def _find_result(self, symbol: str, timeframe: str) -> Optional[SymbolResult]:
        canonical = self._canonical_symbol(symbol)
        timeframe_results = self._results.get(timeframe, {})
        direct = timeframe_results.get(canonical) or timeframe_results.get(symbol)
        if direct is not None:
            return direct
        for result_symbol, result in timeframe_results.items():
            if self._canonical_symbol(result_symbol) == canonical:
                return result
        return None

    def _queue_chart_scan(self, symbol: str, timeframe: str) -> None:
        """Warm one missing chart without blocking the HTTP request thread."""
        if timeframe not in TIMEFRAMES or self._scanning:
            return

        canonical = self._canonical_symbol(symbol)
        key = (canonical, timeframe)
        with self._chart_lock:
            if key in self._chart_pending:
                return
            self._chart_pending.add(key)

        ticker = next(
            (candidate for candidate in NIFTY236_SYMBOLS if self._canonical_symbol(candidate) == canonical),
            symbol if symbol.upper().endswith(".NS") else f"{symbol}.NS",
        )

        def warm() -> None:
            try:
                result = self._fetch_and_scan(ticker, timeframe)
                if result is not None:
                    with self._results_lock:
                        published = self._results.get(timeframe, {}).copy()
                        published[canonical] = result
                        self._results[timeframe] = published
                    with self._chart_lock:
                        self._chart_cache.pop(key, None)
            except Exception as exc:
                logger.warning("Chart warm-up failed for %s/%s: %s", ticker, timeframe, exc)
            finally:
                with self._chart_lock:
                    self._chart_pending.discard(key)

        self._chart_executor.submit(warm)

    def _format_active_trade(self, result: SymbolResult) -> dict | None:
        """Extract and format active trade info for the frontend from a SymbolResult."""
        if not (result.active_trade or (result.latest.get("signal") in ("BUY", "SELL") and result.latest.get("option_type") in ("CE", "PE"))):
            return None
            
        at = result.active_trade
        lt = result.latest
        if at:
            raw_dir = str(at.direction)
            display_dir = "BUY" if raw_dir in ("LONG", "BUY") else "SELL"
            ep = _safe(at.entry_price)
            et = _ts(at.entry_time)
            st = _ts(at.signal_time)
            s1 = _safe(at.sl1)
            s2 = _safe(at.sl2)
            t1 = _safe(at.tp1)
            t2 = _safe(at.tp2)
            t3 = _safe(at.tp3)
            ts = _safe(at.tsl)
            t1h = bool(at.t1_hit)
            t2h = bool(at.t2_hit)
            t3h = bool(at.t3_hit)
            stp = str(at.setup) if hasattr(at, "setup") else ""
            def _opt_val(key):
                val = _safe(getattr(at, key, None))
                return val if val is not None else _safe(lt.get(key))

            o_type = str(getattr(at, "option_type", "") or lt.get("option_type", ""))
            o_strike = _opt_val("option_strike")
            o_sym = str(getattr(at, "option_symbol", "") or lt.get("option_symbol", ""))
            o_entry = _opt_val("option_entry")
            o_sl1 = _opt_val("option_sl1")
            o_sl2 = _opt_val("option_sl2")
            o_tsl = _opt_val("option_tsl")
            o_tp1 = _opt_val("option_tp1")
            o_tp2 = _opt_val("option_tp2")
            o_tp3 = _opt_val("option_tp3")
        else:
            raw_dir = str(lt.get("active_direction") or lt.get("signal", ""))
            display_dir = "BUY" if raw_dir in ("LONG", "BUY") else ("SELL" if raw_dir in ("SHORT", "SELL") else "")
            ep = _safe(lt.get("entry_price") or lt.get("close"))
            et = _ts(lt.get("timestamp"))
            st = _ts(lt.get("timestamp"))
            s1 = _safe(lt.get("sl1") or lt.get("planned_sl1"))
            s2 = _safe(lt.get("sl2"))
            t1 = _safe(lt.get("tp1") or lt.get("planned_tp1"))
            t2 = _safe(lt.get("tp2") or lt.get("planned_tp2"))
            t3 = _safe(lt.get("tp3") or lt.get("planned_tp3"))
            ts = _safe(lt.get("tsl"))
            t1h = False
            t2h = False
            t3h = False
            stp = str(lt.get("setup", ""))
            o_type = str(lt.get("option_type", ""))
            o_strike = _safe(lt.get("option_strike"))
            o_sym = str(lt.get("option_symbol", ""))
            o_entry = _safe(lt.get("option_entry"))
            o_sl1 = _safe(lt.get("option_sl1"))
            o_sl2 = _safe(lt.get("option_sl2"))
            o_tsl = _safe(lt.get("option_tsl"))
            o_tp1 = _safe(lt.get("option_tp1"))
            o_tp2 = _safe(lt.get("option_tp2"))
            o_tp3 = _safe(lt.get("option_tp3"))
            
        return {
            "direction": display_dir,
            "entry_price": ep,
            "entry_time": et,
            "signal_time": st,
            "sl1": s1,
            "sl2": s2,
            "tp1": t1,
            "tp2": t2,
            "tp3": t3,
            "tsl": ts,
            "t1_hit": t1h,
            "t2_hit": t2h,
            "t3_hit": t3h,
            "setup": stp,
            "score": _signal_score_from_result(result),
            "option_type": o_type,
            "option_strike": o_strike,
            "option_symbol": o_sym,
            "option_entry": o_entry,
            "option_sl1": o_sl1,
            "option_sl2": o_sl2,
            "option_tsl": o_tsl,
            "option_tp1": o_tp1,
            "option_tp2": o_tp2,
            "option_tp3": o_tp3,
        }

    def get_chart_data(self, symbol: str, timeframe: str) -> dict:
        """Return OHLCV candles + signal markers for a symbol/timeframe."""
        canonical = self._canonical_symbol(symbol)
        cache_key = (canonical, timeframe)
        empty = {
            "symbol": symbol, "timeframe": timeframe,
            "candles": [], "signals": [], "macd": [], "current_price": None, "active_trade": None,
            "scan_run_at": _ts(self._last_scan), "loading": True,
        }

        result = self._find_result(symbol, timeframe)
        if result is None:
            self._queue_chart_scan(symbol, timeframe)
            return empty

        result_id = id(result)
        with self._chart_lock:
            cached = self._chart_cache.get(cache_key)
            if cached is not None and cached[0] == result_id:
                return cached[1]

        frame = result.frame
        is_daily = timeframe == "1d"
        candles: list[dict] = []
        for ts, row in frame.iterrows():
            try:
                o  = _safe(row.get("open"))
                h  = _safe(row.get("high"))
                lo = _safe(row.get("low"))
                c  = _safe(row.get("close"))
                v  = _safe(row.get("volume")) or 0.0
                if o and h and lo and c:
                    time_val = _epoch_ist(ts)
                    candles.append({"time": time_val, "open": o, "high": h, "low": lo, "close": c, "volume": v})
            except Exception:
                pass

        # Compute MACD (12, 26, 9) from close prices
        macd_data: list[dict] = []
        if len(candles) > 26:
            try:
                closes = pd.Series([c["close"] for c in candles])
                ema12 = closes.ewm(span=12, adjust=False).mean()
                ema26 = closes.ewm(span=26, adjust=False).mean()
                macd_line = ema12 - ema26
                signal_line = macd_line.ewm(span=9, adjust=False).mean()
                histogram = macd_line - signal_line
                for i, candle in enumerate(candles):
                    mv = _safe(macd_line.iloc[i])
                    ms = _safe(signal_line.iloc[i])
                    mh = _safe(histogram.iloc[i])
                    if mv is not None and ms is not None:
                        macd_data.append({"time": candle["time"], "macd": mv, "signal": ms, "histogram": mh or 0.0})
            except Exception as exc:
                logger.warning(f"MACD computation failed: {exc}")

        scan_run_at = _ts(self._last_scan)
        signals: list[dict] = []
        if "signal" in frame.columns:
            for ts, row in frame.iterrows():
                sig = str(row.get("signal", ""))
                if sig in ("BUY", "SELL"):
                    try:
                        sig_score = _safe(row.get("signal_score")) or 0.0
                        ep = _safe(row.get("entry_price"))
                        cp = _safe(row.get("close"))
                        price = (ep if ep and ep > 0 else cp) or 0.0
                        signals.append({
                            "time":           _epoch_ist(ts),
                            "type":           sig,
                            "price":          price,
                            "tp1":            _safe(row.get("planned_tp1")),
                            "tp2":            _safe(row.get("planned_tp2")),
                            "tp3":            _safe(row.get("planned_tp3")),
                            "sl":             _safe(row.get("planned_sl1")),
                            "score":          sig_score,
                            "setup":          str(row.get("setup", "")),
                            "scan_run_at":    scan_run_at,
                        })
                    except Exception:
                        pass

        active_trade_info = self._format_active_trade(result)
        
        other_active_trades = {}
        for tf in TIMEFRAMES:
            if tf == timeframe:
                continue
            tf_result = self._find_result(symbol, tf)
            if tf_result:
                tf_active = self._format_active_trade(tf_result)
                if tf_active:
                    other_active_trades[tf] = tf_active

        payload = {
            "symbol":        symbol,
            "timeframe":     timeframe,
            "candles":       candles[-500:] if len(candles) > 500 else candles,
            "signals":       signals[-100:] if len(signals) > 100 else signals,
            "current_price": _safe(result.latest.get("close")),
            "active_trade":  active_trade_info,
            "other_active_trades": other_active_trades,
            "scan_run_at":   scan_run_at,
            "loading":       False,
            "macd":          macd_data[-500:] if len(macd_data) > 500 else macd_data,
        }
        encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        with self._chart_lock:
            self._chart_cache[cache_key] = (result_id, payload, encoded)
        return payload

    def get_chart_json(self, symbol: str, timeframe: str) -> str:
        """Return a pre-encoded chart response so repeated polls avoid Pandas and JSON work."""
        payload = self.get_chart_data(symbol, timeframe)
        cache_key = (self._canonical_symbol(symbol), timeframe)
        result = self._find_result(symbol, timeframe)
        if result is not None:
            with self._chart_lock:
                cached = self._chart_cache.get(cache_key)
                if cached is not None and cached[0] == id(result):
                    return cached[2]
        return json.dumps(payload, separators=(",", ":"), allow_nan=False)

    def get_stats(self) -> dict:
        active, pending, buy_s, sell_s = 0, 0, 0, 0
        for tf in TIMEFRAMES:
            for _, result in self._results[tf].items():
                lt = result.latest
                state = str(lt.get("state", ""))
                sig   = str(lt.get("signal", ""))
                if state == "ACTIVE":
                    active += 1
                elif state == "PENDING":
                    pending += 1
                if sig == "BUY":
                    buy_s += 1
                elif sig == "SELL":
                    sell_s += 1

        ms = get_market_status()
        with self._scan_state_lock:
            scan_count = self._scan_count
            scan_latency_ms = self._scan_latency_ms
            scan_started_at = self._scan_started_at
            scanning = self._scanning
        try:
            from data_provider import get_data_health
            data_health = get_data_health()
        except Exception:
            data_health = None
        try:
            from settings_store import get_settings
            _s = get_settings()
            trade_style = str(_s.get("trade_style", "all"))
            active_timeframes = timeframes_for_style(trade_style, list(_s.get("enabled_timeframes", TIMEFRAMES)))
        except Exception:
            trade_style, active_timeframes = "all", list(TIMEFRAMES)
        # The portfolio circuit state was computed and then reachable from no
        # endpoint at all, so with 77 positions open against a cap of 5 nothing
        # in the product said so. Surface it.
        try:
            portfolio = self._portfolio_circuit_state()
        except Exception as exc:
            logger.debug("portfolio circuit state unavailable: %s", exc)
            portfolio = None

        return {
            "total_symbols":   len(SCAN_SYMBOLS),
            "active_trades":   active,
            "pending_signals": pending,
            "buy_signals":     buy_s,
            "sell_signals":    sell_s,
            "portfolio":       portfolio,
            "last_scan":       _ts(self._last_scan),
            "scanning":        scanning,
            "scan_errors":     self._scan_errors,
            "scan_count":      scan_count,
            "scan_latency_ms": scan_latency_ms,
            "scan_started_at": _ts(scan_started_at),
            "timeframes":      TIMEFRAMES,
            # Trade-style selection and the timeframes it scans (UI alignment).
            "trade_style":       trade_style,
            "active_timeframes": active_timeframes,
            "trade_style_options": ["intraday", "intraday_btst", "all"],
            # The dashboard reads breadth from /api/stats; it previously only
            # existed in /api/signals so the widget never rendered.
            "market_breadth":  self._market_breadth(),
            "data_health":     data_health,
            # Session info
            "session_status":  ms["session_status"],
            "market_open":     ms["market_open"],
            "nse_time":        ms["nse_time"],
            "next_open":       ms["next_open"],
            "next_close":      ms["next_close"],
        }

    def get_active_timeframes(self) -> set[str]:
        """Returns a set of timeframes that have at least one ACTIVE or PENDING trade."""
        active_tfs = set()
        for tf in TIMEFRAMES:
            for _, result in self._results[tf].items():
                lt = result.latest
                state = str(lt.get("state", ""))
                if state in ("ACTIVE", "PENDING"):
                    active_tfs.add(tf)
                    break # one is enough to mark the timeframe active
        return active_tfs

    def get_history(self, limit: int = 300, symbol: str | None = None, timeframe: str | None = None) -> dict:
        """Closed (exited / SL / target / repaint) trades from the DB log."""
        limit = max(1, min(int(limit), 1000))
        rows: list[dict] = []
        summary: dict = {}
        db = SessionLocal()
        try:
            query = db.query(Trade).filter(Trade.status == "CLOSED")
            if symbol:
                # Match with and without .NS suffix
                clean = symbol.replace(".NS", "").upper()
                query = query.filter(Trade.symbol.in_([clean, clean + ".NS"]))
            if timeframe:
                query = query.filter(Trade.timeframe == timeframe)
            records = (
                query
                .order_by(Trade.exit_time.desc())
                .limit(limit + 100)  # over-fetch to account for dedup
                .all()
            )
            # Deduplicate: keep only the first occurrence per (symbol, tf, entry_time).
            # Duplicates arise from scan-loop race conditions persisting the same
            # trade closure more than once.
            seen: set[tuple] = set()
            for t in records:
                dedup_key = (t.symbol, t.timeframe, str(t.entry_time), t.direction)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                rows.append({
                    "id": t.id,
                    "symbol": t.symbol.replace(".NS", ""),
                    "timeframe": t.timeframe,
                    "direction": t.direction,
                    "trade_type": t.trade_type,
                    "entry_time": t.entry_time.isoformat() if t.entry_time else None,
                    "entry_price": _tick(_safe(t.entry_price)),
                    "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                    "exit_price": _tick(_safe(t.exit_price)),
                    "sl1": _tick(_safe(t.sl1)),
                    "tp1": _tick(_safe(t.tp1)),
                    "exit_reason": t.exit_reason or "",
                    "pnl_pct": round(t.pnl, 2) if t.pnl is not None else None,
                })
                if len(rows) >= limit:
                    break

            # Headline statistics MUST be computed over the whole closed set,
            # not over the page the UI happened to fetch. The dashboard was
            # deriving them from the most recent `limit` rows with NULL-pnl
            # rows folded in as zero, which reported avg -0.17%/trade against a
            # true -0.54% and never surfaced the cumulative figure at all.
            agg = db.query(Trade).filter(Trade.status == "CLOSED")
            if symbol:
                clean = symbol.replace(".NS", "").upper()
                agg = agg.filter(Trade.symbol.in_([clean, clean + ".NS"]))
            if timeframe:
                agg = agg.filter(Trade.timeframe == timeframe)
            pnls = [float(v) for (v,) in agg.with_entities(Trade.pnl).all() if v is not None]
            unresolved = agg.filter(Trade.pnl.is_(None)).count()
            closed_total = len(pnls) + unresolved
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p < 0]
            summary = {
                "closed_total": closed_total,
                # Trades with no recorded outcome. Counted, never averaged in.
                "unresolved": unresolved,
                "decided": len(pnls),
                "win_rate_pct": round(100.0 * len(wins) / len(pnls), 2) if pnls else None,
                "avg_pnl_pct": round(sum(pnls) / len(pnls), 4) if pnls else None,
                "cumulative_pnl_pct": round(sum(pnls), 2) if pnls else None,
                "avg_win_pct": round(sum(wins) / len(wins), 4) if wins else None,
                "avg_loss_pct": round(sum(losses) / len(losses), 4) if losses else None,
                "profit_factor": (
                    round(sum(wins) / abs(sum(losses)), 3) if wins and losses and sum(losses) else None
                ),
                # Win rate this system must reach to break even at its current
                # payoff ratio — the single most useful number on the page.
                "breakeven_win_rate_pct": (
                    round(100.0 * abs(sum(losses) / len(losses))
                          / ((sum(wins) / len(wins)) + abs(sum(losses) / len(losses))), 2)
                    if wins and losses else None
                ),
                "showing": len(rows),
            }
        except Exception as exc:
            logger.error(f"History query failed: {exc}")
        finally:
            db.close()
        return {"trades": rows, "total": len(rows), "summary": summary}

    def get_symbols(self) -> dict:
        return {
            "symbols":    [s.replace(".NS", "") for s in SCAN_SYMBOLS],
            "timeframes": TIMEFRAMES,
        }
