"""Scanner engine: orchestrates multi-symbol, multi-timeframe APEX scans."""
from __future__ import annotations

import logging
import math
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date as dt_date, time as dt_time
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from apex_python_scanner import ApexConfig, ApexScanner, SymbolResult, ActiveTrade
from data_provider import IST, fetch_ohlcv
from nifty50 import NIFTY50_SYMBOLS, TIMEFRAMES
from database import SessionLocal, Trade, SignalState

logger = logging.getLogger(__name__)

IST_TZ = ZoneInfo("Asia/Kolkata")

# ─── NSE Session Helpers ──────────────────────────────────────────────────────

NSE_OPEN  = dt_time(9, 15)
NSE_CLOSE = dt_time(15, 30)

# NSE trading holidays 2026
NSE_HOLIDAYS_2026: set[dt_date] = {
    dt_date(2026, 1, 26),   # Republic Day
    dt_date(2026, 3, 25),   # Holi
    dt_date(2026, 4, 2),    # Ram Navami
    dt_date(2026, 4, 3),    # Good Friday
    dt_date(2026, 4, 14),   # Ambedkar Jayanti
    dt_date(2026, 5, 1),    # Maharashtra Day
    dt_date(2026, 8, 15),   # Independence Day
    dt_date(2026, 10, 2),   # Gandhi Jayanti
    dt_date(2026, 11, 2),   # Diwali Laxmi Pujan (2026)
    dt_date(2026, 11, 3),   # Diwali Balipratipada
    dt_date(2026, 12, 25),  # Christmas
}


def ist_now() -> datetime:
    return datetime.now(IST_TZ)


def get_market_status() -> dict:
    """Return current NSE market session status."""
    now = ist_now()
    today = now.date()
    t = now.time().replace(tzinfo=None)

    is_weekday = today.weekday() < 5
    is_holiday = today in NSE_HOLIDAYS_2026
    is_open_window = NSE_OPEN <= t <= NSE_CLOSE
    market_open = is_weekday and not is_holiday and is_open_window

    if is_holiday:
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
            if candidate.weekday() < 5 and candidate not in NSE_HOLIDAYS_2026:
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


def scan_interval_secs() -> int:
    """Return the recommended seconds to wait before the next scan."""
    ms = get_market_status()
    if ms["market_open"]:
        return 300    # 5 min during live market
    if ms["session_status"] == "PRE_OPEN":
        return 600    # 10 min in pre-open
    return 3600       # 1 hour outside market (swing signals still matter)


# ─── Per-timeframe APEX configs ───────────────────────────────────────────────

_CONFIGS: dict[str, ApexConfig] = {
    "15m": ApexConfig(
        min_score=65.0,
        conflict_margin=15.0,
        use_htf=True,
        entry_delay_bars=0,
        realistic_fills=True,
        allow_entry_on_last_bar=True,
        use_session=True,
        enforce_market_hours=True,
        max_input_bars=800,
        keep_full_history=True,
    ),
    "1h": ApexConfig(
        min_score=63.0,
        conflict_margin=14.0,
        use_htf=True,
        entry_delay_bars=0,
        realistic_fills=True,
        allow_entry_on_last_bar=True,
        use_session=False,
        max_input_bars=700,
        keep_full_history=True,
    ),
    "4h": ApexConfig(
        min_score=60.0,
        conflict_margin=12.0,
        use_htf=True,
        entry_delay_bars=0,
        realistic_fills=True,
        allow_entry_on_last_bar=True,
        use_session=False,
        max_input_bars=500,
        keep_full_history=True,
    ),
    "1d": ApexConfig(
        min_score=58.0,
        conflict_margin=10.0,
        use_htf=True,
        entry_delay_bars=0,
        realistic_fills=True,
        allow_entry_on_last_bar=True,
        use_session=False,
        max_input_bars=400,
        keep_full_history=True,
    ),
}

_SCANNERS: dict[str, ApexScanner] = {
    tf: ApexScanner(cfg) for tf, cfg in _CONFIGS.items()
}

# Expected typical trade durations per timeframe (hours)
_EXPECTED_DURATION_HRS: dict[str, float] = {
    "15m": 3.0,
    "1h":  12.0,
    "4h":  48.0,
    "1d":  240.0,  # 10 trading days
}


# ─── Value helpers ────────────────────────────────────────────────────────────

def _safe(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def _ts(ts: Any) -> Optional[str]:
    try:
        if isinstance(ts, pd.Timestamp):
            return ts.isoformat()
        return str(ts) if ts else None
    except Exception:
        return None


def _trade_type(tf: str, entry_ts: Optional[pd.Timestamp] = None, direction: str = "", expected_duration_hrs: float = 0.0) -> str:
    """Classify trade dynamically as Intraday, BTST, STBT, or Swing."""
    if pd.isna(entry_ts) or entry_ts is None:
        return "Swing" if tf in ("4h", "1d") else "Intraday"
        
    try:
        now = pd.Timestamp.now(tz=entry_ts.tz) if entry_ts.tz else pd.Timestamp.now()
        
        # If signal is from a previous day, it's definitely a Swing trade.
        if entry_ts.date() < now.date():
            return "Swing"
            
        # Signal was generated TODAY.
        current_hour = now.hour + (now.minute / 60.0)
        remaining_hours = max(0.0, 15.5 - current_hour)
        
        is_late = current_hour >= 15.25  # 15:15 or later
        spans_overnight = expected_duration_hrs > remaining_hours
        
        if is_late or spans_overnight or tf in ("4h", "1d"):
            if direction.upper() in ("BUY", "LONG"):
                return "BTST"
            elif direction.upper() in ("SELL", "SHORT"):
                return "STBT"
            return "Swing"
            
        return "Intraday"
    except Exception:
        return "Swing" if tf in ("4h", "1d") else "Intraday"


def _signal_score_from_result(result: SymbolResult) -> float:
    """
    Get the most meaningful score for a symbol.

    Priority order:
    1. Latest bar's signal_score (if > 0)
    2. Most recent signal_score > 0 anywhere in the frame (for ACTIVE trades
       where the signal fired on a previous bar)
    3. dominant_score from latest bar
    4. Max of bull_score / bear_score
    """
    lt = result.latest

    # 1. Latest bar signal_score
    sc = lt.get("signal_score")
    if sc is not None:
        f = _safe(sc)
        if f is not None and f > 0:
            return f

    # 2. Search backwards in frame for most recent non-zero signal_score
    frame = result.frame
    if frame is not None and not frame.empty and "signal_score" in frame.columns:
        col = frame["signal_score"]
        nonzero = col[col > 0]
        if not nonzero.empty:
            return float(nonzero.iloc[-1])

    # 3. dominant_score
    dom = lt.get("dominant_score")
    if dom is not None:
        f = _safe(dom)
        if f is not None and f > 0:
            return f

    # 4. bull/bear score based on bias
    bias = str(lt.get("bias", ""))
    if "BULL" in bias:
        bs = _safe(lt.get("bull_score"))
        if bs:
            return bs
    elif "BEAR" in bias:
        bs = _safe(lt.get("bear_score"))
        if bs:
            return bs

    # 5. max of whatever scores exist
    candidates = [_safe(lt.get(k)) for k in ("bull_score", "bear_score", "dominant_score")]
    valid = [c for c in candidates if c is not None and c > 0]
    return max(valid) if valid else 0.0


def _daily_move_pct(result: SymbolResult) -> Optional[float]:
    """Today's price move from previous daily close (%)."""
    frame = result.frame
    if frame is None or frame.empty:
        return None
    try:
        dates = frame.index.normalize().unique()
        if len(dates) < 2:
            return 0.0
            
        prev_date = dates[-2]
        prev_close_series = frame[frame.index.normalize() == prev_date]
        if prev_close_series.empty:
            return 0.0
            
        prev_close = _safe(prev_close_series.iloc[-1]["close"])
        current = _safe(result.latest.get("close"))
        
        if not prev_close or not current or prev_close <= 0:
            return None
        return round((current - prev_close) / prev_close * 100, 2)
    except Exception:
        return None


def _trade_age_hrs(entry_time_ts: Any) -> Optional[float]:
    """Hours elapsed since trade entry."""
    if entry_time_ts is None:
        return None
    try:
        now = ist_now()
        if isinstance(entry_time_ts, pd.Timestamp):
            et = entry_time_ts.to_pydatetime()
            if et.tzinfo is None:
                et = et.replace(tzinfo=IST_TZ)
        elif isinstance(entry_time_ts, datetime):
            et = entry_time_ts
            if et.tzinfo is None:
                et = et.replace(tzinfo=IST_TZ)
        else:
            # Try parsing ISO string
            from datetime import timezone
            et = datetime.fromisoformat(str(entry_time_ts))
            if et.tzinfo is None:
                et = et.replace(tzinfo=IST_TZ)
        diff = (now - et).total_seconds() / 3600
        return round(max(0.0, diff), 1)
    except Exception:
        return None


def _eta_hrs(timeframe: str, age_hrs: Optional[float]) -> Optional[float]:
    """Estimated hours remaining in the trade."""
    expected = _EXPECTED_DURATION_HRS.get(timeframe, 24.0)
    if age_hrs is None:
        return round(expected, 1)
    remaining = expected - age_hrs
    return round(remaining, 1)  # can be negative if trade is overdue


# ─── Engine ───────────────────────────────────────────────────────────────────

class ScannerEngine:
    """Maintains scanner state across all symbols and timeframes."""

    def __init__(self) -> None:
        self._results: dict[str, dict[str, SymbolResult]] = {tf: {} for tf in TIMEFRAMES}
        self._scanning: bool = False
        self._last_scan: Optional[datetime] = None
        self._scan_errors: int = 0
        self._pending_events: list[dict] = []   # notification events from last scan

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
                    sl1   = _safe(lt.get("sl1") or lt.get("planned_sl1"))
                    tp1   = _safe(lt.get("tp1") or lt.get("planned_tp1"))
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
        if self._scanning:
            logger.info("Scan already running – skip")
            return
        self._scanning = True
        self._scan_errors = 0
        tfs_to_scan = timeframes or TIMEFRAMES
        ms = get_market_status()
        logger.info(
            f"Starting scan: {len(NIFTY50_SYMBOLS)} symbols × {len(tfs_to_scan)} timeframes "
            f"[session={ms['session_status']}]"
        )
        prev_states = self._snapshot_states()
        new_results: dict[str, dict[str, SymbolResult]] = {tf: {} for tf in tfs_to_scan}
        try:
            from data_provider import prefetch_all_ohlcv
            prefetch_all_ohlcv(NIFTY50_SYMBOLS, tfs_to_scan)
            
            with ThreadPoolExecutor(max_workers=50) as exe:
                futures = {
                    exe.submit(self._fetch_and_scan, sym, tf): (sym, tf)
                    for tf in tfs_to_scan
                    for sym in NIFTY50_SYMBOLS
                }
                for fut in as_completed(futures):
                    sym, tf = futures[fut]
                    try:
                        result = fut.result()
                        if result is not None:
                            display = sym.replace(".NS", "")
                            new_results[tf][display] = result
                    except Exception as exc:
                        logger.error(f"Scan error {sym}/{tf}: {exc}")
                        self._scan_errors += 1
            for tf in tfs_to_scan:
                self._results[tf] = new_results[tf]

            # Process database state tracking (buffering & repaints)
            db = SessionLocal()
            try:
                for tf in tfs_to_scan:
                    for sym, result in new_results[tf].items():
                        self._process_db_state(db, sym, tf, result)
                db.commit()
            except Exception as e:
                db.rollback()
                logger.error(f"DB Error: {e}")
            finally:
                db.close()

            self._last_scan = ist_now()
            self._pending_events = self._diff_states(prev_states)
            logger.info(f"Scan done. errors={self._scan_errors} events={len(self._pending_events)}")
        finally:
            self._scanning = False

    def _process_db_state(self, db, sym, tf, result: SymbolResult):
        from datetime import datetime
        lt = result.latest
        state = str(lt.get("state", ""))
        sig = str(lt.get("signal", ""))
        
        # Parse timestamp safely
        try:
            current_ts = datetime.fromisoformat(str(lt.get("bar_open_time")))
        except:
            current_ts = datetime.now()
            
        active_db_trade = db.query(Trade).filter_by(symbol=sym, timeframe=tf, status="ACTIVE").first()

        # 1. Manage Signal Buffering
        if state == "PENDING" and sig in ("BUY", "SELL"):
            ss = db.query(SignalState).filter_by(symbol=sym, timeframe=tf, candle_timestamp=current_ts).first()
            if not ss:
                ss = SignalState(symbol=sym, timeframe=tf, signal_dir=sig, first_seen_time=datetime.now(), candle_timestamp=current_ts)
                db.add(ss)
                db.flush()
            
            buffer_sec = 90 if tf == "15m" else 300
            if (datetime.now() - ss.first_seen_time).total_seconds() >= buffer_sec and not ss.is_executed:
                ss.is_executed = True
                
                # Force Execute Trade!
                expected_dur = _EXPECTED_DURATION_HRS[tf] if tf in _EXPECTED_DURATION_HRS else 2.0
                tr = Trade(
                    symbol=sym, timeframe=tf, direction=sig, 
                    entry_time=datetime.now(), entry_price=lt.get("close"),
                    sl1=lt.get("sl1"), tp1=lt.get("tp1"), tsl=lt.get("sl1"),
                    trade_type=_trade_type(tf, datetime.now(), sig, expected_dur)
                )
                db.add(tr)
                active_db_trade = tr # set this so the logic below picks it up immediately

        # 2. Sync Active DB Trade with Engine Result
        if active_db_trade:
            if state == "CLOSED" and lt.get("exit_reason"):
                # Normal exit (SL/TP)
                active_db_trade.status = "CLOSED"
                active_db_trade.exit_reason = lt.get("exit_reason")
                active_db_trade.exit_time = datetime.now()
                active_db_trade.exit_price = lt.get("close")
            elif state not in ("ACTIVE", "PENDING", "CLOSED"):
                # Repaint Exit! Engine sees no trade, but we had one live.
                active_db_trade.status = "CLOSED"
                active_db_trade.exit_reason = "REPAINT_EXIT"
                active_db_trade.exit_time = datetime.now()
                active_db_trade.exit_price = lt.get("close")
                
                # Override result so diff_states emits EXIT
                lt["state"] = "CLOSED"
                lt["exit_reason"] = "REPAINT_EXIT"
            else:
                # Still Active. Override engine values with true DB entry price
                lt["state"] = "ACTIVE"
                lt["entry_price"] = active_db_trade.entry_price
                lt["active_direction"] = active_db_trade.direction
                
                # Recalculate PNL using the true DB entry price
                current_price = float(lt.get("close", 0.0))
                if active_db_trade.direction in ("LONG", "BUY"):
                    pnl_pct = (current_price - active_db_trade.entry_price) / active_db_trade.entry_price * 100.0 if active_db_trade.entry_price > 0 else 0.0
                    pnl_abs = current_price - active_db_trade.entry_price
                else:
                    pnl_pct = (active_db_trade.entry_price - current_price) / active_db_trade.entry_price * 100.0 if active_db_trade.entry_price > 0 else 0.0
                    pnl_abs = active_db_trade.entry_price - current_price
                lt["live_pnl_pct"] = pnl_pct
                lt["live_pnl_abs"] = pnl_abs

                if result.active_trade:
                    result.active_trade.entry_price = active_db_trade.entry_price
                    active_db_trade.tsl = result.active_trade.tsl
                else:
                    # Engine is still PENDING, we must fabricate active_trade for the frontend payload
                    result.active_trade = ActiveTrade(
                        direction="LONG" if active_db_trade.direction == "BUY" else "SHORT",
                        signal_time=current_ts, entry_time=active_db_trade.entry_time, entry_bar=0,
                        entry_price=active_db_trade.entry_price, sl1=active_db_trade.sl1, sl2=active_db_trade.sl1,
                        tsl=active_db_trade.tsl, tp1=active_db_trade.tp1, tp2=active_db_trade.tp1, tp3=active_db_trade.tp1,
                        setup="DB_BUFFERED", stop_mode="ATR", option_type="CE", option_strike=0,
                        peak_price=active_db_trade.entry_price, trough_price=active_db_trade.entry_price
                    )

    def _fetch_and_scan(self, symbol: str, timeframe: str) -> Optional[SymbolResult]:
        df = fetch_ohlcv(symbol, timeframe)
        if df is None or len(df) < 50:
            return None
        display = symbol.replace(".NS", "")
        try:
            return _SCANNERS[timeframe].run_symbol(
                display, df, asset_type="stock"
            )
        except Exception as exc:
            logger.warning(f"ApexScanner failed {symbol}/{timeframe}: {exc}")
            return None

    # ── Public API ────────────────────────────────────────────────────────────

    def get_signals(
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

                signals.append({
                    "symbol": sym,
                    "timeframe": tf,
                    "direction": display_dir,
                    "score": round(score, 1),
                    "state": state,
                    "entry": _safe(lt.get("entry_price") or lt.get("close")),
                    "entry_price": _safe(lt.get("entry_price") or lt.get("close")),
                    "sl1": _safe(lt.get("sl1") or lt.get("planned_sl1")),
                    "sl2": _safe(lt.get("sl2")),
                    "tp1": _safe(lt.get("tp1") or lt.get("planned_tp1")),
                    "tp2": _safe(lt.get("tp2") or lt.get("planned_tp2")),
                    "tp3": _safe(lt.get("tp3") or lt.get("planned_tp3")),
                    "setup": str(lt.get("setup", "")),
                    "timestamp": _ts(lt.get("timestamp")),
                    "signal_time": _ts(lt.get("timestamp")),
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
                    # ── New professional fields ──
                    "intraday_or_swing": _trade_type(tf, lt.get("timestamp"), str(lt.get("signal", "")), _EXPECTED_DURATION_HRS[tf]),
                    "daily_move_pct": dmove,
                    "trade_age_hrs": age,
                    "eta_hrs": eta,
                    "expected_duration_hrs": _EXPECTED_DURATION_HRS[tf],
                    "active_timeframes": active_tfs_by_sym.get(sym, {}),
                })

        buy_n  = sum(1 for s in signals if s["direction"] == "BUY")
        sell_n = sum(1 for s in signals if s["direction"] == "SELL")
        active_n = sum(1 for s in signals if s["state"] == "ACTIVE")
        
        # Calculate market breadth based on 1D signals
        breadth_buy = 0
        breadth_sell = 0
        if "1d" in self._results:
            for sym, result in self._results["1d"].items():
                b = result.latest.get("bias", "")
                if b == "BULLISH": breadth_buy += 1
                elif b == "BEARISH": breadth_sell += 1
                
        breadth_total = breadth_buy + breadth_sell
        market_breadth = {
            "bullish_pct": round(breadth_buy / breadth_total * 100) if breadth_total > 0 else 50,
            "bearish_pct": round(breadth_sell / breadth_total * 100) if breadth_total > 0 else 50,
        }

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

    def get_active_trades(self) -> dict:
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
                        "sl1": _safe(active.sl1) or 0.0,
                        "sl2": _safe(active.sl2),
                        "tp1": _safe(active.tp1) or 0.0,
                        "tp2": _safe(active.tp2),
                        "tp3": _safe(active.tp3),
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
        rows: list[dict] = []
        tfs = [timeframe] if (timeframe and timeframe in TIMEFRAMES) else TIMEFRAMES

        for tf in tfs:
            for sym, result in self._results[tf].items():
                lt = result.latest
                score = _signal_score_from_result(result)
                rows.append({
                    "symbol": sym,
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
                    "intraday_or_swing": _trade_type(tf, lt.get("timestamp"), str(lt.get("signal", "")), _EXPECTED_DURATION_HRS[tf]),
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

    def get_chart_data(self, symbol: str, timeframe: str) -> dict:
        """Return OHLCV candles + signal markers for a symbol/timeframe."""
        empty = {
            "symbol": symbol, "timeframe": timeframe,
            "candles": [], "signals": [], "current_price": None, "active_trade": None,
        }

        tf_results = self._results.get(timeframe, {})

        # Exact match first, then case-insensitive
        result = tf_results.get(symbol)
        if result is None:
            sym_up = symbol.upper().replace(".NS", "")
            for k, v in tf_results.items():
                if k.upper().replace(".NS", "") == sym_up:
                    result = v
                    break

        if result is None:
            return empty

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

        return {
            "symbol":        symbol,
            "timeframe":     timeframe,
            "candles":       candles[-500:] if len(candles) > 500 else candles,
            "signals":       signals[-100:] if len(signals) > 100 else signals,
            "current_price": _safe(result.latest.get("close")),
            "active_trade":  active_trade_info,
            "scan_run_at":   scan_run_at,
        }

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
        return {
            "total_symbols":   len(NIFTY50_SYMBOLS),
            "active_trades":   active,
            "pending_signals": pending,
            "buy_signals":     buy_s,
            "sell_signals":    sell_s,
            "last_scan":       _ts(self._last_scan),
            "scanning":        self._scanning,
            "scan_errors":     self._scan_errors,
            "timeframes":      TIMEFRAMES,
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

    def get_symbols(self) -> dict:
        return {
            "symbols":    [s.replace(".NS", "") for s in NIFTY50_SYMBOLS],
            "timeframes": TIMEFRAMES,
        }
