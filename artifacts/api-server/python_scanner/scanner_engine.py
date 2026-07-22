"""Scanner engine: orchestrates multi-symbol, multi-timeframe APEX scans."""
from __future__ import annotations

import logging
import math
import os
import time
import uuid
import json
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from datetime import datetime, date as dt_date, time as dt_time
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

# ─── NSE Session Helpers ──────────────────────────────────────────────────────

NSE_OPEN  = dt_time(9, 15)
NSE_CLOSE = dt_time(15, 30)

# Backwards-compatible alias; the authoritative list lives in market_calendar.
NSE_HOLIDAYS_2026: set[dt_date] = holidays_for(2026)


def ist_now() -> datetime:
    return datetime.now(IST_TZ)


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

    is_weekday = today.weekday() < 5
    holiday_today = is_holiday(today)
    is_open_window = NSE_OPEN <= t <= NSE_CLOSE
    market_open = is_weekday and not holiday_today and is_open_window

    if holiday_today:
        status = "HOLIDAY"
    elif not is_weekday:
        status = "WEEKEND"
    elif t < NSE_OPEN:
        status = "PRE_OPEN"
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
                candidate_dt += pd.Timedelta(days=1)
                candidate = candidate_dt.date()
                continue
            if candidate.weekday() < 5 and not is_holiday(candidate):
                next_open_str = candidate_dt.isoformat()
                break
            import datetime as _dt
            candidate = candidate + _dt.timedelta(days=1)

    return {
        "session_status": status,
        "market_open": market_open,
        "nse_time": now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "next_open": next_open_str,
        "next_close": next_close_str,
    }


def scan_interval_secs(now: Optional[datetime] = None) -> int:
    """Return the recommended seconds to wait before the next scan."""
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
        allow_entry_on_last_bar=True,
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
        allow_entry_on_last_bar=True,
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
        allow_entry_on_last_bar=True,
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
        allow_entry_on_last_bar=True,
        use_session=False,
        max_input_bars=300,
        keep_full_history=False,
        fixed_sl_pct=0.0,  # No fixed SL hardcap - always ATR-based
        strict_ohlcv=False,
    ),
}

def build_configs() -> dict[str, ApexConfig]:
    """Apply user settings (settings_store) on top of the per-TF baselines."""
    import copy
    from settings_store import get_settings

    s = get_settings()
    configs: dict[str, ApexConfig] = {}
    for tf, base in _CONFIGS.items():
        cfg = copy.copy(base)  # dataclass with slots; shallow copy is fine
        # Keep scanner gates identical to the user's APEX Hybrid Pro settings.
        # Per-timeframe baselines still supply data/history sizing only.
        cfg.min_score = float(s["min_score"])
        cfg.conflict_margin = float(s["conflict_margin"])
        cfg.min_adx = float(s["min_adx"])
        cfg.use_htf = bool(s["use_htf"])
        cfg.signal_cooldown = int(s["signal_cooldown"])
        cfg.atr_mult = float(s["atr_mult"])
        if s["sl_mode"] == "fixed":
            cfg.fixed_sl_pct = float(s["fixed_sl_pct"])
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
        cfg.validate()
        configs[tf] = cfg
    return configs


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
    return None


def _trade_age_hrs(entry_ts: Any) -> Optional[float]:
    """Return elapsed hours since entry_ts in Asia/Kolkata time."""
    if entry_ts is None or pd.isna(entry_ts) or entry_ts == "" or entry_ts == "—":
        return None
    try:
        now_dt = datetime.now(IST_TZ)
        dt = pd.to_datetime(entry_ts)
        if dt.tz is None:
            dt = dt.tz_localize(IST_TZ)
        else:
            dt = dt.tz_convert(IST_TZ)
        diff_sec = (now_dt - dt).total_seconds()
        return round(max(0.0, diff_sec / 3600.0), 1)
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
                    return "SWING"
            else:
                now_dt = datetime.now(IST_TZ)
                if now_dt.date() != dt_in.date():
                    return "SWING"
        except Exception:
            pass
    return "INTRADAY"


def _signal_score_from_result(result: SymbolResult) -> float:
    """Extract score from result and decay exponentially by bars_since_signal."""
    if not result or not result.latest:
        return 0.0
    raw = _safe(result.latest.get("score")) or 0.0
    bars_since = _safe(result.latest.get("bars_since_signal")) or 0.0
    if bars_since > 0:
        return round(raw * (0.95 ** bars_since), 1)
    return round(raw, 1)


def _scan_preloaded(
    symbol: str,
    tf: str,
    df: Optional[pd.DataFrame],
    settings_rev: int = 0,
) -> Optional[SymbolResult]:
    """Top-level worker function executed inside ProcessPoolExecutor."""
    if df is None or df.empty:
        return None
    try:
        configs = build_configs()
        cfg = configs.get(tf)
        if cfg is None:
            return None
        scanner = ApexScanner(cfg)
        return scanner.run_symbol(symbol, df)
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
        self._scan_executor: Optional[ProcessPoolExecutor] = None

        # Keep analytics route latency deterministic even while the first scan
        # is still building. These empty snapshots are replaced atomically when
        # a complete scan generation is ready.
        for tenure in ("1d", "7d", "30d", "90d", "180d", "365d"):
            self.get_analytics(tenure)

    def _get_scan_executor(self) -> ProcessPoolExecutor:
        if self._scan_executor is None:
            default_workers = max(1, min(4, (os.cpu_count() or 2) - 1))
            worker_count = max(1, int(os.getenv("APEX_SCAN_WORKERS", str(default_workers))))
            self._scan_executor = ProcessPoolExecutor(max_workers=worker_count)
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
        for tf in TIMEFRAMES:
            for sym, result in self._results[tf].items():
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

        for tf in TIMEFRAMES:
            for sym, result in self._results[tf].items():
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
        scan_started_monotonic = time.monotonic()
        from settings_store import get_settings, revision as settings_revision
        enabled = get_settings()["enabled_timeframes"]
        tfs_to_scan = timeframes or [tf for tf in TIMEFRAMES if tf in enabled] or TIMEFRAMES
        settings_rev = settings_revision()
        ms = get_market_status()
        logger.info(
            f"Starting scan: {len(SCAN_SYMBOLS)} symbols × {len(tfs_to_scan)} timeframes "
            f"[session={ms['session_status']}]"
        )
        prev_states = self._snapshot_states()
        new_results: dict[str, dict[str, SymbolResult]] = {tf: {} for tf in tfs_to_scan}
        try:
            from data_provider import prefetch_all_ohlcv

            # Warm and publish one timeframe at a time. Copy-on-write result
            # dictionaries let API readers see completed symbols immediately
            # without ever iterating a dictionary that is being mutated.
            for tf in tfs_to_scan:
                prefetch_all_ohlcv(SCAN_SYMBOLS, [tf])
                executor = self._get_scan_executor()
                symbol_iterator = iter(SCAN_SYMBOLS)
                pending: dict[Any, str] = {}
                max_pending = max(2, int(os.getenv("APEX_SCAN_WORKERS", "4")) * 2)

                def submit_next() -> bool:
                    try:
                        symbol = next(symbol_iterator)
                    except StopIteration:
                        return False
                    frame = fetch_ohlcv(symbol, tf)
                    if frame is None or len(frame) < 50:
                        self._scan_errors += 1
                        return True
                    pending[executor.submit(_scan_preloaded, symbol, tf, frame, settings_rev)] = symbol
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
                                published = self._results.get(tf, {}).copy()
                                published[display] = result
                                self._results[tf] = published
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
                for zombie in db.query(Trade).filter(Trade.status == "ACTIVE", Trade.symbol.like("%.NS")).all():
                    zombie.status = "CLOSED"
                    zombie.exit_reason = "LEGACY_SYMBOL_CLEANUP"
                    zombie.exit_time = ist_now().replace(tzinfo=None)
                db.commit()
            except Exception as e:
                db.rollback()
                logger.error(f"DB Error: {e}")
            finally:
                db.close()

            self._last_scan = ist_now()
            self._pending_events = self._diff_states(prev_states)
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

    def _process_db_state(self, db, sym, tf, result: SymbolResult):
        from datetime import datetime
        lt = result.latest
        state = str(lt.get("state", ""))
        sig = str(lt.get("signal", ""))

        # All DB times are naive IST (SQLite drops tzinfo; mixing server-local
        # datetime.now() with tz-aware bar times broke candle_timestamp lookups).
        def naive_ist_now() -> datetime:
            return ist_now().replace(tzinfo=None)

        try:
            parsed = pd.Timestamp(str(lt.get("bar_open_time")))
            parsed = parsed.tz_localize(IST_TZ) if parsed.tz is None else parsed.tz_convert(IST_TZ)
            current_ts = parsed.to_pydatetime().replace(tzinfo=None)
        except Exception:
            current_ts = naive_ist_now()

        try:
            active_db_trade = db.query(Trade).filter_by(symbol=sym, timeframe=tf, status="ACTIVE").first()
        except Exception as e:
            logger.error(f"Database error fetching active trade for {sym}/{tf}: {e}")
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
                    active_db_trade = Trade(
                        symbol=sym, timeframe=tf, direction=direction,
                        entry_time=pd.Timestamp(active.entry_time).to_pydatetime().replace(tzinfo=None),
                        entry_price=active.entry_price, status="ACTIVE",
                    )
                    db.add(active_db_trade)

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
                matching_exit = None
                for record in reversed(result.trades or []):
                    record_entry = pd.Timestamp(record.entry_time).to_pydatetime().replace(tzinfo=None)
                    if record_entry == active_db_trade.entry_time:
                        matching_exit = record
                        break
                if matching_exit is not None:
                    active_db_trade.status = "CLOSED"
                    active_db_trade.exit_reason = matching_exit.exit_reason
                    active_db_trade.exit_price = matching_exit.exit_price
                    active_db_trade.exit_time = pd.Timestamp(matching_exit.exit_time).to_pydatetime().replace(tzinfo=None)
                    active_db_trade.pnl = matching_exit.pnl_pct
                elif lt.get("exit_reason"):
                    logger.warning("Ignoring unpaired APEX exit for %s/%s; no matching APEX trade record", sym, tf)
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
        for t in TIMEFRAMES:
            if t in self._results:
                for sym, res in self._results[t].items():
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
            for sym, result in self._results[tf].items():
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
                    continue  # FLAT with no signal – skip

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
                })

        # Newest signal first, then highest score within identical timestamps.
        signals.sort(key=lambda s: (s.get("signal_time") or "", s.get("score") or 0.0), reverse=True)

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
        }

    def _market_breadth(self) -> dict:
        """Bullish/bearish share of the universe from the daily-timeframe bias.

        The engine emits "STR BULL"/"MILD BULL"/"STR BEAR"/"MILD BEAR"/"NEUTRAL";
        the old exact comparison against "BULLISH"/"BEARISH" never matched.
        """
        breadth_buy = breadth_sell = 0
        for _sym, result in self._results.get("1d", {}).items():
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

        for tf in TIMEFRAMES:
            for sym, result in self._results[tf].items():
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

        for tf in tfs:
            for sym, result in self._results[tf].items():
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

        cache_key = (days_limit, _ts(self._last_scan))
        if cache_key in self._analytics_cache:
            return self._analytics_cache[cache_key]

        now = ist_now()
        cutoff_dt = now - pd.Timedelta(days=days_limit)

        timeframe_stats: dict[str, dict] = {}
        all_trades_by_tf: dict[str, list[dict]] = {tf: [] for tf in TIMEFRAMES}
        all_trades_flat: list[dict] = []

        total_active = 0
        total_signals = 0

        for tf in TIMEFRAMES:
            for sym, result in self._results.get(tf, {}).items():
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
        valid_sharpes = [timeframe_stats[tf]["sharpe_ratio"] for tf in TIMEFRAMES if timeframe_stats[tf]["sharpe_ratio"] != 0]
        all_sharpe = round(float(np.mean(valid_sharpes)), 2) if valid_sharpes else 0.0
        valid_pfs = [timeframe_stats[tf]["profit_factor"] for tf in TIMEFRAMES if timeframe_stats[tf]["profit_factor"] > 0]
        all_pf = round(float(np.mean(valid_pfs)), 2) if valid_pfs else 0.0

        timeframe_stats["ALL"] = {
            "timeframe": "ALL",
            "num_trades": all_closed,
            "wins": all_wins,
            "losses": all_losses,
            "win_rate_pct": all_win_rate,
            "profit_factor": all_pf,
            "sharpe_ratio": all_sharpe,
            "total_pnl_pct": all_total_pnl,
            "avg_win_pct": round(float(np.mean([timeframe_stats[tf]["avg_win_pct"] for tf in TIMEFRAMES if timeframe_stats[tf]["avg_win_pct"] > 0])), 2) if any(timeframe_stats[tf]["avg_win_pct"] > 0 for tf in TIMEFRAMES) else 0.0,
            "avg_loss_pct": round(float(np.mean([timeframe_stats[tf]["avg_loss_pct"] for tf in TIMEFRAMES if timeframe_stats[tf]["avg_loss_pct"] > 0])), 2) if any(timeframe_stats[tf]["avg_loss_pct"] > 0 for tf in TIMEFRAMES) else 0.0,
            "payoff_ratio": round(float(np.mean([timeframe_stats[tf]["payoff_ratio"] for tf in TIMEFRAMES if timeframe_stats[tf]["payoff_ratio"] > 0])), 2) if any(timeframe_stats[tf]["payoff_ratio"] > 0 for tf in TIMEFRAMES) else 0.0,
            "half_kelly_pct": round(float(np.mean([timeframe_stats[tf]["half_kelly_pct"] for tf in TIMEFRAMES if timeframe_stats[tf]["half_kelly_pct"] > 0])), 1) if any(timeframe_stats[tf]["half_kelly_pct"] > 0 for tf in TIMEFRAMES) else 0.0,
            "insufficient_data": all_closed < 5,
        }

        # Strategy Type Breakdown (Intraday vs Swing vs BTST/STBT).
        # _trade_type returns title-case values; the old comparison against
        # "INTRADAY" and the setup-string BTST search never matched anything.
        categories = {"INTRADAY": [], "SWING": [], "BTST_STBT": []}
        for tr in all_trades_flat:
            trade_type = str(tr.get("intraday_or_swing", ""))
            tf = tr["timeframe"]
            if trade_type in ("BTST", "STBT"):
                categories["BTST_STBT"].append(tr)
            elif tf == "15m" or trade_type == "Intraday":
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

        daily_pnl += round(live_active_pnl_pct, 2)
        weekly_pnl += round(live_active_pnl_pct, 2)
        monthly_pnl += round(live_active_pnl_pct, 2)

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

        payload = {
            "timeframe_breakdown": timeframe_stats,
            "strategy_breakdown": strategy_breakdown,
            "period_breakdown": period_breakdown,
            "equity_curve": equity_curve,
            "sector_performance": sectors_list,
            "summary": {
                "total_symbols": len(SCAN_SYMBOLS),
                "total_active_trades": total_active,
                "total_signals": total_signals,
                "overall_win_rate_pct": all_win_rate,
                "overall_profit_factor": all_pf,
                "overall_sharpe_ratio": all_sharpe,
                "total_historical_trades": all_closed,
                "selected_tenure": raw_tenure.upper(),
                "max_tenure_limit": "365D",
                "last_scan": _ts(self._last_scan),
                # Provenance so nobody mistakes these for broker-verified fills:
                # trades are engine-simulated on a rolling candle window.
                "data_basis": "SIMULATED_ROLLING_WINDOW",
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
                    self._results.setdefault(timeframe, {})[canonical] = result
                    with self._chart_lock:
                        self._chart_cache.pop(key, None)
            except Exception as exc:
                logger.warning("Chart warm-up failed for %s/%s: %s", ticker, timeframe, exc)
            finally:
                with self._chart_lock:
                    self._chart_pending.discard(key)

        self._chart_executor.submit(warm)

    def get_chart_data(self, symbol: str, timeframe: str) -> dict:
        """Return OHLCV candles + signal markers for a symbol/timeframe."""
        canonical = self._canonical_symbol(symbol)
        cache_key = (canonical, timeframe)
        empty = {
            "symbol": symbol, "timeframe": timeframe,
            "candles": [], "signals": [], "current_price": None, "active_trade": None,
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
        candles: list[dict] = []
        for ts, row in frame.iterrows():
            try:
                epoch = int(pd.Timestamp(ts).timestamp())
                o  = _safe(row.get("open"))
                h  = _safe(row.get("high"))
                lo = _safe(row.get("low"))
                c  = _safe(row.get("close"))
                v  = _safe(row.get("volume")) or 0.0
                if o and h and lo and c:
                    candles.append({"time": epoch, "open": o, "high": h, "low": lo, "close": c, "volume": v})
            except Exception:
                pass

        scan_run_at = _ts(self._last_scan)
        signals: list[dict] = []
        if "signal" in frame.columns:
            for ts, row in frame.iterrows():
                sig = str(row.get("signal", ""))
                if sig in ("BUY", "SELL"):
                    try:
                        sig_score = _safe(row.get("signal_score")) or 0.0
                        # Fix: entry_price column may be 0; fall back to close
                        ep = _safe(row.get("entry_price"))
                        cp = _safe(row.get("close"))
                        price = (ep if ep and ep > 0 else cp) or 0.0
                        signals.append({
                            "time":           int(pd.Timestamp(ts).timestamp()),
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

        active_trade_info = None
        if result.active_trade:
            at = result.active_trade
            # Normalise direction: LONG→BUY, SHORT→SELL
            raw_dir = str(at.direction)
            display_dir = "BUY" if raw_dir in ("LONG", "BUY") else "SELL"
            active_trade_info = {
                "direction":   display_dir,
                "entry_price": _safe(at.entry_price),
                "entry_time":  _ts(at.entry_time),
                "signal_time": _ts(at.signal_time),
                "sl1":  _safe(at.sl1),
                "sl2":  _safe(at.sl2),
                "tp1":  _safe(at.tp1),
                "tp2":  _safe(at.tp2),
                "tp3":  _safe(at.tp3),
                "tsl":  _safe(at.tsl),
                "t1_hit": bool(at.t1_hit),
                "t2_hit": bool(at.t2_hit),
                "t3_hit": bool(at.t3_hit),
                "setup": str(at.setup) if hasattr(at, "setup") else "",
                "score": _signal_score_from_result(result),
            }

        payload = {
            "symbol":        symbol,
            "timeframe":     timeframe,
            "candles":       candles[-500:] if len(candles) > 500 else candles,
            "signals":       signals[-100:] if len(signals) > 100 else signals,
            "current_price": _safe(result.latest.get("close")),
            "active_trade":  active_trade_info,
            "scan_run_at":   scan_run_at,
            "loading":       False,
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
        return {
            "total_symbols":   len(SCAN_SYMBOLS),
            "active_trades":   active,
            "pending_signals": pending,
            "buy_signals":     buy_s,
            "sell_signals":    sell_s,
            "last_scan":       _ts(self._last_scan),
            "scanning":        scanning,
            "scan_errors":     self._scan_errors,
            "scan_count":      scan_count,
            "scan_latency_ms": scan_latency_ms,
            "scan_started_at": _ts(scan_started_at),
            "timeframes":      TIMEFRAMES,
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

    def get_history(self, limit: int = 300) -> dict:
        """Closed (exited / SL / target / repaint) trades from the DB log."""
        limit = max(1, min(int(limit), 1000))
        rows: list[dict] = []
        db = SessionLocal()
        try:
            records = (
                db.query(Trade)
                .filter(Trade.status == "CLOSED")
                .order_by(Trade.exit_time.desc())
                .limit(limit)
                .all()
            )
            for t in records:
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
        except Exception as exc:
            logger.error(f"History query failed: {exc}")
        finally:
            db.close()
        return {"trades": rows, "total": len(rows)}

    def get_symbols(self) -> dict:
        return {
            "symbols":    [s.replace(".NS", "") for s in SCAN_SYMBOLS],
            "timeframes": TIMEFRAMES,
        }
