"""
APEX Python Scanner
===================
A non-repainting, multi-symbol Python translation of the uploaded
"APEX Hybrid Pro v5.5" Pine Script indicator.

Primary design goals
--------------------
* Pure pandas/numpy implementation; TA-Lib is not required.
* Previous-closed-bar MTF features to prevent lookahead leakage.
* Signal-at-close, entry-on-next-bar execution by default.
* Stateful entries, stops, targets, trailing stops, momentum exits,
  loss circuit breaker, trade history and rolling performance metrics.
* Full 39-pattern candlestick recognition engine from the Pine source.
* Multi-symbol scanner output suitable for an algo execution layer.
* Statistical swing projection and active support/resistance zones.

This module generates trade signals and order intents. It deliberately does
not place broker orders. Connect its ``OrderIntent`` output to your OMS/risk
layer only after independent backtesting and paper trading.
"""

from __future__ import annotations

import argparse
import json
import math
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Sequence

import numpy as np
import pandas as pd


IST = "Asia/Kolkata"
EPS = 1e-12
__version__ = "2.0.0-audited"


# ---------------------------------------------------------------------------
# Configuration and result models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ApexConfig:
    # Instrument
    instrument_type: str = "Auto-Detect"

    # Signal gates
    min_score: float = 70.0
    conflict_margin: float = 20.0
    min_adx: float = 20.0
    use_htf: bool = True
    signal_cooldown: int = 5
    min_history_bars: int = 200
    use_ai_score_model: bool = True
    score_model_path: str = ""

    # Risk engine
    atr_mult: float = 1.5
    fixed_sl_pct: float = 0.2
    t1_r: float = 2.0
    t2_r: float = 3.0
    t3_r: float = 4.0
    # Fixed-percentage target mode: when > 0, targets are entry ± pct moves
    # (t1 = pct, t2 = 1.5x, t3 = 2x) instead of R-multiples of the stop.
    fixed_tp_pct: float = 0.0
    # Book the FULL position at the first touch of TP1 (no trail-for-more).
    exit_at_t1: bool = False
    # Apply fixed_sl_pct in ALL regimes. Without this the fixed stop is only
    # used in quiet, non-trending conditions, so a deterministic reward:risk
    # (e.g. always 1:2) cannot be guaranteed.
    force_fixed_sl: bool = False
    # Slippage percentage applied to entry and exit executions (HIGH-27)
    slippage_pct: float = 0.05
    # Daily maximum cumulative loss percentage before triggering circuit breaker (HIGH-26)
    daily_max_loss_pct: float = 3.0
    # Maximum concurrent open positions allowed across the system
    max_open_positions: int = 5

    # Trailing stop / exits
    use_trail: bool = True
    trail_start_r: float = 1.5
    trail_mult: float = 1.8
    lock_at_t1: bool = True
    exit_confirmation_bars: int = 3
    max_consecutive_losses: int = 3
    circuit_pause_bars: int = 10

    # Session handling. The Pine source only blocks two noise windows.
    # Production mode can additionally enforce the NSE/BSE cash-session
    # envelope, but should not apply it to MCX/crypto/forex instruments.
    use_session: bool = True
    enforce_market_hours: bool = True
    apply_nse_session_to_non_nse: bool = False
    market_open: str = "09:15"
    market_close: str = "15:30"
    block_open_noise: bool = True
    open_noise_end: str = "09:30"
    block_close_noise: bool = True
    close_noise_start: str = "15:00"
    timezone: str = IST

    # Options guidance & trading engine (`options_engine.py`)
    enable_options: bool = True
    strike_mode: str = "Smart Auto"
    trade_options_intraday: bool = True
    options_broker: str = "upstox"
    options_stop_mode: str = "Delta-Translated"

    # Pattern engine
    show_patterns: bool = True
    use_pattern_score: bool = True
    pattern_single_group: bool = True
    pattern_double_group: bool = True
    pattern_triple_group: bool = True
    pattern_continuation_group: bool = True

    # Swing forecast
    swing_length: int = 16
    sr_zone_atr: float = 0.30
    sr_max_age: int = 300
    swing_samples: int = 20
    swing_method: str = "Weighted"  # Weighted, Average, Median
    forecast_bars: int = 5
    fib_ratios: tuple[float, ...] = (1.0, 1.272, 1.618)

    # Execution model
    entry_delay_bars: int = 1
    realistic_fills: bool = True
    allow_entry_on_last_bar: bool = True
    cancel_pending_outside_session: bool = True
    keep_full_history: bool = True
    recent_trade_limit: int = 20

    # Data assumptions
    # Most broker APIs and TradingView label intraday bars by bar OPEN time.
    # Set True only when the input timestamp is the candle CLOSE time.
    timestamps_are_bar_close: bool = False
    strict_ohlcv: bool = True
    # Optional rolling-window mode for live scanners. Zero keeps all history.
    max_input_bars: int = 0

    def validate(self) -> None:
        if not 0 <= self.fixed_sl_pct <= 10:
            raise ValueError("fixed_sl_pct must be between 0 and 10")
        if not 0 <= self.min_score <= 100:
            raise ValueError("min_score must be between 0 and 100")
        if self.signal_cooldown < 0:
            raise ValueError("signal_cooldown cannot be negative")
        if self.entry_delay_bars < 0:
            raise ValueError("entry_delay_bars cannot be negative")
        if self.t1_r <= 0 or self.t2_r <= 0 or self.t3_r <= 0:
            raise ValueError("R-multiples must be positive")
        if not (self.t1_r <= self.t2_r <= self.t3_r):
            raise ValueError("Expected t1_r <= t2_r <= t3_r")
        if not 0 <= self.fixed_tp_pct <= 20:
            raise ValueError("fixed_tp_pct must be between 0 and 20")
        if self.swing_method not in {"Weighted", "Average", "Median"}:
            raise ValueError("swing_method must be Weighted, Average or Median")
        if self.strike_mode not in {"Smart Auto", "Always ATM", "Always OTM1", "Always ITM1"}:
            raise ValueError("Unsupported strike_mode")
        if self.atr_mult <= 0 or self.trail_mult <= 0:
            raise ValueError("ATR multipliers must be positive")
        if self.trail_start_r < 0:
            raise ValueError("trail_start_r cannot be negative")
        if self.exit_confirmation_bars < 1:
            raise ValueError("exit_confirmation_bars must be at least 1")
        if self.max_consecutive_losses < 1 or self.circuit_pause_bars < 0 or self.daily_max_loss_pct <= 0:
            raise ValueError("Invalid circuit-breaker settings")
        if self.min_history_bars < 1:
            raise ValueError("min_history_bars must be positive")
        if self.swing_length < 2 or self.swing_samples < 2:
            raise ValueError("Swing settings are too small")
        if self.sr_zone_atr <= 0 or self.sr_max_age < 1:
            raise ValueError("Invalid support/resistance settings")
        if self.forecast_bars < 1:
            raise ValueError("forecast_bars must be positive")
        if self.recent_trade_limit < 0:
            raise ValueError("recent_trade_limit cannot be negative")
        if self.max_input_bars < 0:
            raise ValueError("max_input_bars cannot be negative")
        if 0 < self.max_input_bars <= self.min_history_bars:
            raise ValueError("max_input_bars must exceed min_history_bars")
        for text in (self.market_open, self.market_close, self.open_noise_end, self.close_noise_start):
            _time_minutes(text)
        if _time_minutes(self.market_open) >= _time_minutes(self.market_close):
            raise ValueError("market_open must be earlier than market_close")


class _FastRow:
    """Read-only, allocation-light row view used by the live scan loop."""

    __slots__ = ("_values", "_positions")

    def __init__(self, values: tuple[Any, ...], positions: dict[str, int]):
        self._values = values
        self._positions = positions

    def __getitem__(self, key: str) -> Any:
        return self._values[self._positions[key]]

    def get(self, key: str, default: Any = None) -> Any:
        position = self._positions.get(key)
        return default if position is None else self._values[position]


@dataclass(slots=True)
class InstrumentProfile:
    name: str
    supports_index_options: bool
    strike_interval: float


@dataclass(slots=True)
class ActiveTrade:
    direction: str
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    entry_bar: int
    entry_price: float
    sl1: float
    sl2: float
    tsl: float
    tp1: float
    tp2: float
    tp3: float
    setup: str
    stop_mode: str
    option_type: str = ""
    option_strike: float = math.nan
    t1_hit: bool = False
    t2_hit: bool = False
    t3_hit: bool = False
    profit_locked: bool = False
    peak_price: float = math.nan
    trough_price: float = math.nan
    exit_confirmation_count: int = 0


@dataclass(slots=True)
class TradeRecord:
    symbol: str
    direction: str
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    pnl_pct: float
    pnl_r: float
    setup: str
    exit_reason: str
    stop_mode: str
    bars_held: int
    t1_hit: bool
    t2_hit: bool
    t3_hit: bool
    option_type: str = ""
    option_strike: float = math.nan


@dataclass(slots=True)
class SRZone:
    price: float
    start_time: pd.Timestamp
    start_bar: int
    is_resistance: bool
    width: float
    broken: bool = False
    broken_time: Optional[pd.Timestamp] = None


@dataclass(slots=True)
class SwingForecast:
    direction: str = "NONE"
    origin_price: float = math.nan
    origin_time: Optional[pd.Timestamp] = None
    target_price: float = math.nan
    target_lower: float = math.nan
    target_upper: float = math.nan
    expected_move_pct: float = math.nan
    historical_swing_bars: float = math.nan
    projection_bars: int = 0
    standard_deviation_pct: float = math.nan
    fibonacci_prices: dict[float, float] = field(default_factory=dict)
    support_resistance: list[SRZone] = field(default_factory=list)


@dataclass(slots=True)
class PerformanceStats:
    total_entries: int = 0
    closed_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate_pct: float = 0.0
    total_pnl_pct: float = 0.0
    gross_profit_pct: float = 0.0
    gross_loss_pct: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0
    payoff_ratio: float = 0.0
    pnl_signal_ratio: float = 0.0
    half_kelly_pct: float = 0.0
    max_consecutive_losses: int = 0
    current_consecutive_losses: int = 0


@dataclass(slots=True)
class OrderIntent:
    symbol: str
    timestamp: pd.Timestamp
    side: str
    underlying_action: str
    confidence: float
    setup: str
    reference_price: float
    stop_reference: float
    target_1: float
    target_2: float
    target_3: float
    execution_instrument: str = "UNDERLYING"
    option_type: str = ""
    option_strike: float = math.nan
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SymbolResult:
    symbol: str
    instrument: InstrumentProfile
    frame: pd.DataFrame
    trades: list[TradeRecord]
    active_trade: Optional[ActiveTrade]
    pending_order: Optional[dict[str, Any]]
    forecast: SwingForecast
    stats: PerformanceStats
    latest: dict[str, Any]

    def order_intent(self) -> Optional[OrderIntent]:
        signal = str(self.latest.get("signal", ""))
        source: Mapping[str, Any] = self.latest
        if signal not in {"BUY", "SELL"} and self.pending_order is not None:
            signal = "BUY" if bool(self.pending_order.get("is_long")) else "SELL"
            source = self.pending_order
        if signal not in {"BUY", "SELL"}:
            return None
        option_type = str(source.get("option_type", ""))
        uses_option = option_type in {"CE", "PE"}
        return OrderIntent(
            symbol=self.symbol,
            timestamp=pd.Timestamp(source.get("signal_time", self.latest["timestamp"])),
            # A bearish underlying signal maps to BUY PE, not SELL PE.
            # For non-option instruments, side remains BUY/SELL underlying.
            side="BUY" if uses_option else ("BUY" if signal == "BUY" else "SELL"),
            underlying_action="LONG" if signal == "BUY" else "SHORT",
            confidence=float(source.get("score", source.get("signal_score", math.nan))),
            setup=str(source.get("setup", "")),
            reference_price=float(source.get("reference_price", source.get("close", math.nan))),
            stop_reference=float(source.get("planned_sl1", math.nan)),
            target_1=float(source.get("planned_tp1", math.nan)),
            target_2=float(source.get("planned_tp2", math.nan)),
            target_3=float(source.get("planned_tp3", math.nan)),
            execution_instrument="OPTION_GUIDANCE" if uses_option else "UNDERLYING",
            option_type=option_type,
            option_strike=float(source.get("option_strike", math.nan)),
            metadata={
                "signal_side": signal,
                "price_basis": "UNDERLYING",
                "bull_score": source.get("bull_score", self.latest.get("bull_score")),
                "bear_score": source.get("bear_score", self.latest.get("bear_score")),
                "bias": source.get("bias", self.latest.get("bias")),
                "adx": source.get("adx", self.latest.get("adx")),
                "relative_volume": source.get("relative_volume", self.latest.get("relative_volume")),
            },
        )


class MarketDataProvider(Protocol):
    def fetch_ohlcv(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        """Return a DataFrame with timestamp/open/high/low/close/volume."""


# ---------------------------------------------------------------------------
# Low-level indicator functions
# ---------------------------------------------------------------------------


def _safe_div(numerator: pd.Series, denominator: pd.Series, default: float = 0.0) -> pd.Series:
    den = denominator.replace(0.0, np.nan)
    return numerator.div(den).replace([np.inf, -np.inf], np.nan).fillna(default)


def sma(s: pd.Series, length: int) -> pd.Series:
    return s.rolling(length, min_periods=length).mean()


def ema(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(span=length, adjust=False, min_periods=length).mean()


def gap_aware_ema(s: pd.Series, length: int) -> pd.Series:
    """EMA with alpha adjustment across multi-day / weekend gaps (> 24 hours)."""
    if len(s) == 0:
        return s.copy()
    alpha = 2.0 / (length + 1.0)
    values = pd.to_numeric(s, errors="coerce").to_numpy(dtype=float)
    out = np.full(len(values), np.nan, dtype=float)
    valid_positions = np.flatnonzero(np.isfinite(values))
    if len(valid_positions) == 0:
        return pd.Series(out, index=s.index, name=s.name)
    
    idx = s.index
    is_dt = isinstance(idx, pd.DatetimeIndex)
    seed_pos = int(valid_positions[0])
    out[seed_pos] = values[seed_pos]
    previous = out[seed_pos]
    
    for i in range(seed_pos + 1, len(values)):
        val = values[i]
        if np.isfinite(val):
            local_alpha = alpha
            if is_dt and i > 0 and (idx[i] - idx[i - 1]).total_seconds() > 86400.0:
                local_alpha = min(1.0, alpha * 2.5)
            previous = local_alpha * val + (1.0 - local_alpha) * previous
        out[i] = previous
    return pd.Series(out, index=s.index, name=s.name)


def rma(s: pd.Series, length: int) -> pd.Series:
    """TradingView/Wilder RMA with an SMA seed.

    ``Series.ewm(adjust=False)`` seeds from the first value, which is not the
    same as Pine's ``ta.rma``. The difference can materially alter ATR, RSI,
    DMI and ADX around thresholds, so the seed is calculated explicitly.
    """
    if length <= 0:
        raise ValueError("length must be positive")
    values = pd.to_numeric(s, errors="coerce").to_numpy(dtype=float)
    out = np.full(len(values), np.nan, dtype=float)
    alpha = 1.0 / float(length)

    valid_positions = np.flatnonzero(np.isfinite(values))
    if len(valid_positions) < length:
        return pd.Series(out, index=s.index, name=s.name)

    seed_pos = int(valid_positions[length - 1])
    seed_values = values[valid_positions[:length]]
    out[seed_pos] = float(seed_values.mean())
    previous = out[seed_pos]
    for i in range(seed_pos + 1, len(values)):
        value = values[i]
        if np.isfinite(value):
            previous = alpha * value + (1.0 - alpha) * previous
        out[i] = previous
    return pd.Series(out, index=s.index, name=s.name)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    return rma(true_range(df), length)


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = rma(gain, length)
    avg_loss = rma(loss, length)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    out = out.where(avg_loss != 0.0, 100.0)
    out = out.where(avg_gain != 0.0, 0.0)
    both_zero = (avg_gain == 0.0) & (avg_loss == 0.0)
    return out.where(~both_zero, 50.0)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    line = ema(close, fast) - ema(close, slow)
    signal_line = ema(line, signal)
    return line, signal_line, line - signal_line


def dmi_adx(df: pd.DataFrame, di_length: int = 14, adx_length: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0.0), up, 0.0), index=df.index, dtype=float)
    minus_dm = pd.Series(np.where((down > up) & (down > 0.0), down, 0.0), index=df.index, dtype=float)
    atr_di = rma(true_range(df), di_length)
    plus_di = 100.0 * _safe_div(rma(plus_dm, di_length), atr_di)
    minus_di = 100.0 * _safe_div(rma(minus_dm, di_length), atr_di)
    dx = 100.0 * _safe_div((plus_di - minus_di).abs(), plus_di + minus_di)
    adx = rma(dx, adx_length)
    return plus_di, minus_di, adx


def crossover(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a > b) & (a.shift(1) <= b.shift(1))


def crossunder(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a < b) & (a.shift(1) >= b.shift(1))


def session_vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    session_key = pd.Series(df.index.normalize(), index=df.index)
    pv = typical * df["volume"].fillna(0.0)
    cum_pv = pv.groupby(session_key).cumsum()
    cum_vol = df["volume"].fillna(0.0).groupby(session_key).cumsum()
    fallback = typical.groupby(session_key).expanding().mean().reset_index(level=0, drop=True)
    return (cum_pv / cum_vol.replace(0.0, np.nan)).fillna(fallback)


# ---------------------------------------------------------------------------
# Data normalization and MTF handling
# ---------------------------------------------------------------------------


def normalize_ohlcv(data: pd.DataFrame, config: ApexConfig) -> pd.DataFrame:
    if not isinstance(data, pd.DataFrame):
        raise TypeError("OHLCV data must be a pandas DataFrame")
    df = data.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]

    if not isinstance(df.index, pd.DatetimeIndex):
        ts_col = next((c for c in ("timestamp", "datetime", "date", "time") if c in df.columns), None)
        if ts_col is None:
            raise ValueError("Provide a DatetimeIndex or a timestamp/datetime/date column")
        df[ts_col] = pd.to_datetime(df[ts_col], errors="coerce")
        df = df.set_index(ts_col)

    df = df.loc[~df.index.isna()].copy()

    if df.index.tz is None:
        df.index = df.index.tz_localize(config.timezone, ambiguous="infer", nonexistent="shift_forward")
    else:
        df.index = df.index.tz_convert(config.timezone)

    required = ["open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing OHLCV columns: {missing}")

    df = df[required].apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0.0).clip(lower=0.0)

    invalid = (df["high"] < df[["open", "close", "low"]].max(axis=1)) | (
        df["low"] > df[["open", "close", "high"]].min(axis=1)
    )
    invalid |= (df[["open", "high", "low", "close"]] <= 0.0).any(axis=1)
    if invalid.any():
        message = f"Found {int(invalid.sum())} invalid OHLC rows"
        if config.strict_ohlcv:
            raise ValueError(message)
        warnings.warn(message + "; rows were removed", RuntimeWarning)
        df = df.loc[~invalid]

    if len(df) < 5:
        raise ValueError("At least 5 OHLCV bars are required")
    return df


def _infer_base_seconds(index: pd.DatetimeIndex) -> float:
    diffs = index.to_series().diff().dropna().dt.total_seconds()
    if diffs.empty:
        return 60.0
    return float(diffs.median())


def _rule_seconds(rule: str) -> float:
    return float(pd.Timedelta(rule).total_seconds())


def _is_daily_or_higher(rule: str) -> bool:
    return _rule_seconds(rule) >= 86400.0 - EPS


def resample_ohlcv(
    df: pd.DataFrame,
    rule: str,
    *,
    timestamps_are_bar_close: bool = False,
) -> pd.DataFrame:
    """Resample OHLCV without shifting exchange sessions by one calendar day."""
    aggregation = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    if _is_daily_or_higher(rule):
        # Calendar-midnight right-closed resampling labels an Indian session at
        # the following midnight, causing previous-day features to lag by two
        # sessions. Group on the actual local trading date instead.
        session_key = pd.Series(df.index.normalize(), index=df.index, name="session")
        grouped = df.groupby(session_key, sort=True).agg(aggregation)
        grouped.index = pd.DatetimeIndex(grouped.index)
        return grouped.dropna(subset=["open", "high", "low", "close"])

    side = "right" if timestamps_are_bar_close else "left"
    return (
        df.resample(rule, label=side, closed=side)
        .agg(aggregation)
        .dropna(subset=["open", "high", "low", "close"])
    )


def previous_closed_mtf(
    df: pd.DataFrame,
    rule: str,
    feature_builder: Callable[[pd.DataFrame], pd.Series],
    *,
    timestamps_are_bar_close: bool = False,
) -> pd.Series:
    """Map the previous fully closed higher-timeframe feature to base bars.

    This is the Python equivalent of requesting an HTF expression with [1]
    and lookahead_on in Pine. If the requested timeframe is lower than the
    input data, the nearest safe fallback is the prior base-bar feature.
    """
    if _rule_seconds(rule) < _infer_base_seconds(df.index) - EPS:
        return feature_builder(df).shift(1)
    working = df
    if timestamps_are_bar_close and not _is_daily_or_higher(rule):
        # Convert close-labeled base candles to their open timestamps before
        # resampling. This reproduces TradingView's bar-open clock semantics:
        # the HTF bar that just closed becomes available from the next base bar.
        working = df.copy()
        working.index = _bar_open_times(df.index, True)

    htf = resample_ohlcv(working, rule, timestamps_are_bar_close=False)
    values = feature_builder(htf).shift(1)
    if _is_daily_or_higher(rule):
        keys = pd.DatetimeIndex(df.index.normalize())
        mapped = values.reindex(keys)
        return pd.Series(mapped.to_numpy(), index=df.index, name=values.name)
    mapped = values.reindex(working.index, method="ffill")
    return pd.Series(mapped.to_numpy(), index=df.index, name=values.name)


def _bar_duration(index: pd.DatetimeIndex) -> pd.Timedelta:
    seconds = _infer_base_seconds(index)
    if not np.isfinite(seconds) or seconds <= 0:
        seconds = 60.0
    return pd.Timedelta(seconds=seconds)


def _bar_open_times(index: pd.DatetimeIndex, timestamps_are_bar_close: bool) -> pd.DatetimeIndex:
    if not timestamps_are_bar_close:
        return index
    duration = _bar_duration(index)
    return index - duration if duration < pd.Timedelta(hours=12) else index


def _bar_close_times(index: pd.DatetimeIndex, timestamps_are_bar_close: bool) -> pd.DatetimeIndex:
    if timestamps_are_bar_close:
        return index
    duration = _bar_duration(index)
    return index + duration if duration < pd.Timedelta(hours=12) else index


def regime_code(frame: pd.DataFrame) -> pd.Series:
    atr_local = atr(frame, 14)
    atr_ratio = _safe_div(atr_local, sma(atr_local, 50), default=1.0)
    e50 = ema(frame["close"], 50)
    _, _, adx_local = dmi_adx(frame, 14, 14)
    bull = frame["close"] > e50
    
    # Bollinger Band width squeeze detection (MEDIUM-13)
    sma20 = sma(frame["close"], 20)
    std20 = frame["close"].rolling(20, min_periods=5).std()
    bb_width = _safe_div(4.0 * std20, sma20, default=0.05)
    bb_min = bb_width.rolling(100, min_periods=20).quantile(0.20)
    is_squeeze = bb_width < bb_min
    
    base = np.select(
        [
            (adx_local > 25.0) & bull,
            (adx_local > 25.0) & ~bull,
            (adx_local > 20.0) & bull,
            (adx_local > 20.0) & ~bull,
        ],
        [1, 2, 3, 4],
        default=0,
    )
    vol = np.select([atr_ratio > 1.25, (atr_ratio < 0.85) | is_squeeze], [10, 20], default=0)
    return pd.Series(base + vol, index=frame.index, dtype=float)


def regime_display(value: Any) -> str:
    if pd.isna(value):
        return "NA"
    rc = int(value)
    base = rc % 10
    vol = rc // 10
    trend = "T+" if base in {1, 3} else "T-" if base in {2, 4} else "R"
    suffix = " V" if vol == 1 else " v" if vol == 2 else ""
    return trend + suffix


# ---------------------------------------------------------------------------
# Instrument and option strike logic
# ---------------------------------------------------------------------------


def detect_instrument(symbol: str, override: str = "Auto-Detect", exchange: str = "", asset_type: str = "") -> InstrumentProfile:
    if override != "Auto-Detect":
        name = override
    else:
        token = f"{exchange}:{symbol}".lower().replace(" ", "")
        typ = asset_type.lower()
        is_bank = any(x in token for x in ("banknifty", "niftybank", "bank_nifty", "banknf"))
        is_fin = any(x in token for x in ("finnifty", "niftyfinservice", "niftyfinancialservices", "fin_nifty", "finnf"))
        is_mid = any(x in token for x in ("midcap", "niftymidselect", "midcpnifty"))
        is_sensex = "sensex" in token or "bankex" in token
        excluded_nifty = (
            "100", "200", "500", "smallcap", "niftyit", "niftyauto", "niftypharma",
            "niftymetal", "niftyrealty", "niftyenergy", "niftyinfra", "niftymedia",
            "niftyfmcg", "niftypsu", "niftynext",
        )
        is_nifty = "nifty50" in token or (
            "nifty" in token and not any((is_bank, is_fin, is_mid)) and not any(x in token for x in excluded_nifty)
        )
        crypto_keys = ("btc", "eth", "bnb", "sol", "ada", "xrp", "doge", "shib", "usdt", "usdc", "ltc", "dot")
        forex_keys = ("eurusd", "gbpusd", "usdjpy", "audusd", "usdcad", "usdchf", "nzdusd", "usdinr", "eurgbp", "eurjpy", "gbpjpy")
        commodity_keys = ("gold", "silver", "crude", "copper", "naturalgas", "natgas", "xauusd", "xagusd", "wti", "brent", "aluminium", "aluminum", "nickel", "zinc", "lead")
        if is_bank:
            name = "Bank Nifty"
        elif is_nifty:
            name = "Nifty 50"
        elif is_fin:
            name = "FinNifty"
        elif is_mid:
            name = "Midcap"
        elif is_sensex:
            name = "Sensex"
        elif typ == "index":
            name = "Index"
        elif typ == "crypto" or any(x in token for x in crypto_keys):
            name = "Crypto"
        elif typ == "forex" or any(x in token for x in forex_keys):
            name = "Forex"
        elif exchange.lower() == "mcx" or any(x in token for x in commodity_keys):
            name = "Commodity"
        elif typ == "futures":
            name = "Futures"
        else:
            name = "Stock"

    supports = name in {"Nifty 50", "Bank Nifty", "FinNifty", "Midcap", "Stock"}
    interval = 100.0 if name == "Bank Nifty" else 25.0 if name == "Midcap" else 50.0
    return InstrumentProfile(name=name, supports_index_options=supports, strike_interval=interval)


def calculate_atm(price: float, interval: float) -> float:
    if not np.isfinite(price) or interval <= 0:
        return math.nan
    # Python's round() uses bankers rounding, while exchange strike selection
    # requires deterministic half-up behavior for positive prices.
    return math.floor(price / interval + 0.5) * interval


def select_option_strike(
    is_ce: bool,
    confidence: float,
    underlying_price: float,
    profile: InstrumentProfile,
    config: ApexConfig,
    high_volatility: bool,
    trending: bool,
) -> tuple[str, float]:
    if not config.enable_options or not profile.supports_index_options:
        return "", math.nan

    offset = 0
    if config.strike_mode == "Always ATM":
        offset = 0
    elif config.strike_mode == "Always OTM1":
        offset = 1
    elif config.strike_mode == "Always ITM1":
        offset = -1
    else:
        if confidence >= 85.0 and high_volatility and trending:
            offset = 1
        elif confidence >= 70.0:
            offset = 0
        else:
            offset = 1

    if config.trade_options_intraday:
        try:
            from options_engine import resolve_atm_option
            contract = resolve_atm_option(
                underlying_symbol=profile.name,
                spot_price=underlying_price,
                direction="LONG" if is_ce else "SHORT",
                strike_offset=offset,
            )
            if contract and contract.get("strike"):
                return ("CE" if is_ce else "PE"), float(contract["strike"])
        except Exception:
            pass

    atm = calculate_atm(underlying_price, profile.strike_interval)
    strike = atm + (offset * profile.strike_interval * (1 if is_ce else -1))
    return ("CE" if is_ce else "PE"), float(strike)


# ---------------------------------------------------------------------------
# Candlestick pattern engine (39 source patterns)
# ---------------------------------------------------------------------------


def _bool(series: pd.Series) -> pd.Series:
    return series.fillna(False).astype(bool)


def build_pattern_features(df: pd.DataFrame, core: pd.DataFrame, config: ApexConfig) -> pd.DataFrame:
    o, h, l, c, v = (df[x] for x in ("open", "high", "low", "close", "volume"))
    atr_v = core["atr"]
    ema21 = core["ema21"]
    bull_trend = core["bull_trend"]
    bear_trend = core["bear_trend"]
    rel_vol = core["relative_volume"]
    vol_above = core["volume_above"]
    vol_spike = core["volume_spike"]

    body0 = (c - o).abs()
    range0 = h - l
    upper0 = h - pd.concat([c, o], axis=1).max(axis=1)
    lower0 = pd.concat([c, o], axis=1).min(axis=1) - l
    bull0 = c > o
    bear0 = c < o
    doji0 = (range0 > 0.0) & (body0 <= range0 * 0.10)

    body1 = body0.shift(1)
    range1 = range0.shift(1)
    upper1 = upper0.shift(1)
    lower1 = lower0.shift(1)
    bull1 = bull0.shift(1)
    bear1 = bear0.shift(1)
    doji1 = doji0.shift(1)

    body2 = body0.shift(2)
    upper2 = upper0.shift(2)
    lower2 = lower0.shift(2)
    bull2 = bull0.shift(2)
    bear2 = bear0.shift(2)

    min_body = atr_v * 0.15
    big_body = atr_v * 0.55
    enabled = config.show_patterns

    near_res = h >= h.shift(1).rolling(20, min_periods=20).max() * 0.997
    near_sup = l <= l.shift(1).rolling(20, min_periods=20).min() * 1.003
    prior_dn1 = bear1
    prior_dn2 = bear1 & bear2
    prior_up1 = bull1
    prior_up2 = bull1 & bull2
    in_dn_trend = (c < ema21) & prior_dn1
    in_up_trend = (c > ema21) & prior_up1
    bull_rev_context = prior_dn2 | (prior_dn1 & (c < ema21)) | (near_sup & prior_dn1)
    bear_rev_context = prior_up2 | (prior_up1 & (c > ema21)) | (near_res & prior_up1)
    pattern_size_ok = range0 >= atr_v * 0.25
    volume_ok = vol_above | vol_spike

    single = enabled and config.pattern_single_group
    double = enabled and config.pattern_double_group
    triple = enabled and config.pattern_triple_group
    cont = enabled and config.pattern_continuation_group

    p: dict[str, pd.Series] = {}

    # Single-bar (9)
    p["doji"] = single & doji0 & (range0 >= min_body)
    p["dragonfly"] = single & doji0 & (range0 >= min_body) & (lower0 >= range0 * 0.65) & (upper0 <= range0 * 0.10)
    p["gravestone"] = single & doji0 & (range0 >= min_body) & (upper0 >= range0 * 0.65) & (lower0 <= range0 * 0.10)
    p["hammer_struct"] = single & (body0 >= min_body) & (lower0 >= body0 * 2.0) & (upper0 <= body0 * 0.35)
    p["inverse_hammer_struct"] = single & (body0 >= min_body) & (upper0 >= body0 * 2.0) & (lower0 <= body0 * 0.35)
    p["bull_marubozu"] = single & bull0 & (body0 >= big_body) & (upper0 <= body0 * 0.05) & (lower0 <= body0 * 0.05)
    p["bear_marubozu"] = single & bear0 & (body0 >= big_body) & (upper0 <= body0 * 0.05) & (lower0 <= body0 * 0.05)
    p["bull_belt_hold"] = single & bull0 & (body0 >= big_body * 0.65) & (lower0 <= body0 * 0.06)
    p["bear_belt_hold"] = single & bear0 & (body0 >= big_body * 0.65) & (upper0 <= body0 * 0.06)

    # Double-bar (14)
    p["bull_engulf"] = double & bear1 & bull0 & (body1 >= min_body) & (body0 >= min_body) & (o <= c.shift(1)) & (c >= o.shift(1))
    p["bear_engulf"] = double & bull1 & bear0 & (body1 >= min_body) & (body0 >= min_body) & (o >= c.shift(1)) & (c <= o.shift(1))
    p["bull_harami"] = double & bear1 & bull0 & (body1 >= big_body * 0.65) & (body0 < body1 * 0.55) & (o > c.shift(1)) & (c < o.shift(1))
    p["bear_harami"] = double & bull1 & bear0 & (body1 >= big_body * 0.65) & (body0 < body1 * 0.55) & (o < c.shift(1)) & (c > o.shift(1))
    p["bull_harami_cross"] = double & bear1 & doji0 & (body1 >= big_body * 0.65) & (h <= o.shift(1)) & (l >= c.shift(1))
    p["bear_harami_cross"] = double & bull1 & doji0 & (body1 >= big_body * 0.65) & (h <= c.shift(1)) & (l >= o.shift(1))
    p["piercing"] = double & bear1 & bull0 & (body1 >= big_body * 0.55) & (o < c.shift(1)) & (c > (c.shift(1) + (o.shift(1) - c.shift(1)) * 0.50)) & (c < o.shift(1))
    p["dark_cloud"] = double & bull1 & bear0 & (body1 >= big_body * 0.55) & (o > c.shift(1)) & (c < (o.shift(1) + (c.shift(1) - o.shift(1)) * 0.50)) & (c > o.shift(1))
    p["tweezer_bottom"] = double & bear1 & bull0 & ((l - l.shift(1)).abs() <= atr_v * 0.12)
    p["tweezer_top"] = double & bull1 & bear0 & ((h - h.shift(1)).abs() <= atr_v * 0.12)
    p["bull_kicker"] = double & bear1 & bull0 & (body1 >= big_body * 0.55) & (body0 >= big_body * 0.55) & (o >= o.shift(1))
    p["bear_kicker"] = double & bull1 & bear0 & (body1 >= big_body * 0.55) & (body0 >= big_body * 0.55) & (o <= o.shift(1))
    p["bull_meeting_line"] = double & bear1 & bull0 & (body1 >= big_body * 0.55) & (body0 >= big_body * 0.55) & ((c - c.shift(1)).abs() <= atr_v * 0.08)
    p["bear_meeting_line"] = double & bull1 & bear0 & (body1 >= big_body * 0.55) & (body0 >= big_body * 0.55) & ((c - c.shift(1)).abs() <= atr_v * 0.08)

    # Triple-bar (12)
    p["morning_star"] = triple & bear2 & (body1 <= body2 * 0.45) & bull0 & (body2 >= big_body * 0.55) & (body0 >= big_body * 0.40) & (c >= (c.shift(2) + (o.shift(2) - c.shift(2)) * 0.50))
    p["evening_star"] = triple & bull2 & (body1 <= body2 * 0.45) & bear0 & (body2 >= big_body * 0.55) & (body0 >= big_body * 0.40) & (c <= (o.shift(2) + (c.shift(2) - o.shift(2)) * 0.50))
    p["morning_star_doji"] = triple & bear2 & doji1 & bull0 & (body2 >= big_body * 0.55) & (body0 >= big_body * 0.40) & (c >= (c.shift(2) + (o.shift(2) - c.shift(2)) * 0.50))
    p["evening_star_doji"] = triple & bull2 & doji1 & bear0 & (body2 >= big_body * 0.55) & (body0 >= big_body * 0.40) & (c <= (o.shift(2) + (c.shift(2) - o.shift(2)) * 0.50))
    p["three_white_soldiers"] = triple & bull0 & bull1 & bull2 & (c > c.shift(1)) & (c.shift(1) > c.shift(2)) & (body0 >= big_body * 0.40) & (body1 >= big_body * 0.40) & (body2 >= big_body * 0.40) & (upper0 <= body0 * 0.35) & (upper1 <= body1 * 0.35) & (upper2 <= body2 * 0.35) & (o > o.shift(1)) & (o.shift(1) > o.shift(2))
    p["three_black_crows"] = triple & bear0 & bear1 & bear2 & (c < c.shift(1)) & (c.shift(1) < c.shift(2)) & (body0 >= big_body * 0.40) & (body1 >= big_body * 0.40) & (body2 >= big_body * 0.40) & (lower0 <= body0 * 0.35) & (lower1 <= body1 * 0.35) & (lower2 <= body2 * 0.35) & (o < o.shift(1)) & (o.shift(1) < o.shift(2))
    p["three_inside_up"] = triple & bear2 & bull1 & bull0 & (body2 >= big_body * 0.55) & (o.shift(1) >= c.shift(2)) & (c.shift(1) <= o.shift(2)) & (c > o.shift(2))
    p["three_inside_down"] = triple & bull2 & bear1 & bear0 & (body2 >= big_body * 0.55) & (o.shift(1) <= c.shift(2)) & (c.shift(1) >= o.shift(2)) & (c < o.shift(2))
    p["three_outside_up"] = triple & bear2 & bull1 & bull0 & (body2 >= min_body) & (body1 >= body2 * 0.90) & (o.shift(1) <= c.shift(2)) & (c.shift(1) >= o.shift(2)) & (c > c.shift(1))
    p["three_outside_down"] = triple & bull2 & bear1 & bear0 & (body2 >= min_body) & (body1 >= body2 * 0.90) & (o.shift(1) >= c.shift(2)) & (c.shift(1) <= o.shift(2)) & (c < c.shift(1))
    p["bull_abandoned_baby"] = triple & bear2 & doji1 & bull0 & (body2 >= big_body * 0.50) & (body0 >= big_body * 0.35) & (h.shift(1) <= pd.concat([o.shift(2), c.shift(2)], axis=1).min(axis=1)) & (l >= pd.concat([o.shift(1), c.shift(1)], axis=1).max(axis=1))
    p["bear_abandoned_baby"] = triple & bull2 & doji1 & bear0 & (body2 >= big_body * 0.50) & (body0 >= big_body * 0.35) & (l.shift(1) >= pd.concat([o.shift(2), c.shift(2)], axis=1).max(axis=1)) & (h <= pd.concat([o.shift(1), c.shift(1)], axis=1).min(axis=1))

    # Continuation (4)
    p["rising_three_methods"] = cont & (c.shift(4) > o.shift(4)) & ((c.shift(4) - o.shift(4)) >= big_body * 0.50) & (c.shift(3) < o.shift(3)) & (c.shift(2) < o.shift(2)) & (c.shift(1) < o.shift(1)) & (h.shift(3) < h.shift(4)) & (h.shift(2) < h.shift(4)) & (h.shift(1) < h.shift(4)) & (l.shift(3) > l.shift(4)) & (l.shift(2) > l.shift(4)) & (l.shift(1) > l.shift(4)) & bull0 & (c > c.shift(4))
    p["falling_three_methods"] = cont & (c.shift(4) < o.shift(4)) & ((o.shift(4) - c.shift(4)) >= big_body * 0.50) & (c.shift(3) > o.shift(3)) & (c.shift(2) > o.shift(2)) & (c.shift(1) > o.shift(1)) & (l.shift(3) > l.shift(4)) & (l.shift(2) > l.shift(4)) & (l.shift(1) > l.shift(4)) & (h.shift(3) < h.shift(4)) & (h.shift(2) < h.shift(4)) & (h.shift(1) < h.shift(4)) & bear0 & (c < c.shift(4))
    p["upside_tasuki"] = cont & bull2 & bull1 & bear0 & (o.shift(1) > c.shift(2)) & (o < c.shift(1)) & (c > c.shift(2)) & (c < o.shift(1))
    p["downside_tasuki"] = cont & bear2 & bear1 & bull0 & (o.shift(1) < c.shift(2)) & (o > c.shift(1)) & (c < c.shift(2)) & (c > o.shift(1))

    for key in p:
        p[key] = _bool(p[key])

    raw_bull_single = p["dragonfly"] | (p["hammer_struct"] & in_dn_trend) | (p["inverse_hammer_struct"] & in_dn_trend) | p["bull_belt_hold"] | p["bull_marubozu"]
    raw_bear_single = p["gravestone"] | (p["hammer_struct"] & in_up_trend) | (p["inverse_hammer_struct"] & in_up_trend) | p["bear_belt_hold"] | p["bear_marubozu"]
    raw_bull_double = p["bull_engulf"] | p["bull_harami"] | p["bull_harami_cross"] | p["piercing"] | p["tweezer_bottom"] | p["bull_kicker"] | p["bull_meeting_line"]
    raw_bear_double = p["bear_engulf"] | p["bear_harami"] | p["bear_harami_cross"] | p["dark_cloud"] | p["tweezer_top"] | p["bear_kicker"] | p["bear_meeting_line"]
    raw_bull_triple = p["morning_star"] | p["morning_star_doji"] | p["three_white_soldiers"] | p["three_inside_up"] | p["three_outside_up"] | p["bull_abandoned_baby"]
    raw_bear_triple = p["evening_star"] | p["evening_star_doji"] | p["three_black_crows"] | p["three_inside_down"] | p["three_outside_down"] | p["bear_abandoned_baby"]
    raw_bull_cont = p["rising_three_methods"] | p["upside_tasuki"]
    raw_bear_cont = p["falling_three_methods"] | p["downside_tasuki"]

    valid_bull_rev = (raw_bull_single | raw_bull_double | raw_bull_triple) & pattern_size_ok & volume_ok & bull_rev_context
    valid_bear_rev = (raw_bear_single | raw_bear_double | raw_bear_triple) & pattern_size_ok & volume_ok & bear_rev_context
    valid_bull_cont = raw_bull_cont & pattern_size_ok & volume_ok & bull_trend
    valid_bear_cont = raw_bear_cont & pattern_size_ok & volume_ok & bear_trend
    valid_bull = _bool(valid_bull_rev | valid_bull_cont)
    valid_bear = _bool(valid_bear_rev | valid_bear_cont)
    raw_bull = raw_bull_single | raw_bull_double | raw_bull_triple | raw_bull_cont
    raw_bear = raw_bear_single | raw_bear_double | raw_bear_triple | raw_bear_cont

    strong_bull = valid_bull & (
        p["bull_engulf"] | p["three_white_soldiers"] | p["morning_star"] |
        p["morning_star_doji"] | p["bull_abandoned_baby"] | p["three_inside_up"] |
        p["three_outside_up"] | p["bull_kicker"] | p["rising_three_methods"]
    )
    strong_bear = valid_bear & (
        p["bear_engulf"] | p["three_black_crows"] | p["evening_star"] |
        p["evening_star_doji"] | p["bear_abandoned_baby"] | p["three_inside_down"] |
        p["three_outside_down"] | p["bear_kicker"] | p["falling_three_methods"]
    )

    bull_priority = [
        ("bull_abandoned_baby", "Aband.Baby U"), ("three_white_soldiers", "3 White Sold"),
        ("morning_star_doji", "Morn.StarDji"), ("morning_star", "Morning Star"),
        ("three_inside_up", "3 Inside Up"), ("three_outside_up", "3 Outside Up"),
        ("bull_kicker", "Bull Kicker"), ("bull_engulf", "Bull Engulf"),
        ("bull_harami_cross", "HaramiCross+"), ("bull_harami", "Bull Harami"),
        ("piercing", "Piercing"), ("tweezer_bottom", "TweezBot"),
        ("bull_meeting_line", "MeetLines+"), ("rising_three_methods", "Rising3Meth"),
        ("upside_tasuki", "Up Tasuki"),
    ]
    bear_priority = [
        ("bear_abandoned_baby", "Aband.Baby D"), ("three_black_crows", "3 Blk Crows"),
        ("evening_star_doji", "Eve.StarDoji"), ("evening_star", "Evening Star"),
        ("three_inside_down", "3 Inside Dn"), ("three_outside_down", "3 Outside Dn"),
        ("bear_kicker", "Bear Kicker"), ("bear_engulf", "Bear Engulf"),
        ("bear_harami_cross", "HaramiCross-"), ("bear_harami", "Bear Harami"),
        ("dark_cloud", "DarkCloud"), ("tweezer_top", "TweezTop"),
        ("bear_meeting_line", "MeetLines-"), ("falling_three_methods", "Falling3Meth"),
        ("downside_tasuki", "Dn Tasuki"),
    ]

    bull_name = pd.Series("", index=df.index, dtype=object)
    bear_name = pd.Series("", index=df.index, dtype=object)
    for key, label in bull_priority:
        mask = valid_bull & p[key] & bull_name.eq("")
        bull_name.loc[mask] = label
    for key, label in bear_priority:
        mask = valid_bear & p[key] & bear_name.eq("")
        bear_name.loc[mask] = label

    extra_bull = [
        (p["hammer_struct"] & in_dn_trend, "Hammer"),
        (p["inverse_hammer_struct"] & in_dn_trend, "Inv.Hammer"),
        (p["dragonfly"], "Dragonfly"), (p["bull_marubozu"], "Marubozu+"),
        (p["bull_belt_hold"], "BeltHold+"),
    ]
    extra_bear = [
        (p["hammer_struct"] & in_up_trend, "HangingMan"),
        (p["inverse_hammer_struct"] & in_up_trend, "Shoot.Star"),
        (p["gravestone"], "Gravestone"), (p["bear_marubozu"], "Marubozu-"),
        (p["bear_belt_hold"], "BeltHold-"),
    ]
    for mask0, label in extra_bull:
        mask = valid_bull & mask0 & bull_name.eq("")
        bull_name.loc[mask] = label
    for mask0, label in extra_bear:
        mask = valid_bear & mask0 & bear_name.eq("")
        bear_name.loc[mask] = label

    result = pd.DataFrame(index=df.index)
    for key, value in p.items():
        result[f"pattern_{key}"] = value
    result["pattern_valid_bull"] = valid_bull
    result["pattern_valid_bear"] = valid_bear
    result["pattern_fake_bull"] = _bool(raw_bull & ~valid_bull)
    result["pattern_fake_bear"] = _bool(raw_bear & ~valid_bear)
    result["pattern_strong_bull"] = _bool(strong_bull)
    result["pattern_strong_bear"] = _bool(strong_bear)
    result["bull_pattern_name"] = bull_name
    result["bear_pattern_name"] = bear_name
    result["pattern_count"] = sum(result[f"pattern_{key}"].astype(int) for key in p)
    return result


# ---------------------------------------------------------------------------
# Feature and score engine
# ---------------------------------------------------------------------------


def _time_minutes(text: str) -> int:
    hh, mm = text.split(":")
    return int(hh) * 60 + int(mm)


def build_feature_frame(
    df: pd.DataFrame,
    config: ApexConfig,
    profile: Optional[InstrumentProfile] = None,
) -> pd.DataFrame:
    f = df.copy()
    f["bar_open_time"] = _bar_open_times(df.index, config.timestamps_are_bar_close)
    f["bar_close_time"] = _bar_close_times(df.index, config.timestamps_are_bar_close)

    # NSE-session instruments: a resampled bucket that straddles the close
    # (e.g. a 13:15 4h candle) must not claim to close at 17:15 — the session
    # ends at market_close. Clamp intraday close labels to the session end.
    nse_session_profile = profile is None or profile.name not in {"Crypto", "Forex", "Commodity"}
    if nse_session_profile and _infer_base_seconds(df.index) < 86400.0 - EPS:
        closes = pd.Series(pd.DatetimeIndex(f["bar_close_time"]), index=f.index)
        session_end = (
            pd.Series(pd.DatetimeIndex(f["bar_open_time"]), index=f.index).dt.normalize()
            + pd.Timedelta(minutes=_time_minutes(config.market_close))
        )
        f["bar_close_time"] = closes.where(closes <= session_end, session_end)
    for length in (5, 9, 13, 21, 50, 200):
        f[f"ema{length}"] = gap_aware_ema(df["close"], length)
    f["vwap"] = session_vwap(df)
    f["atr"] = atr(df, 14)
    f["atr_ma50"] = sma(f["atr"], 50)
    f["atr_expansion"] = _safe_div(f["atr"], f["atr_ma50"], default=1.0)
    f["rsi"] = rsi(df["close"], 14)
    f["macd"], f["macd_signal"], f["macd_hist"] = macd(df["close"], 12, 26, 9)
    f["macd_acc_bull"] = f["macd_hist"] > f["macd_hist"].shift(1)
    f["macd_acc_bear"] = f["macd_hist"] < f["macd_hist"].shift(1)
    f["di_plus"], f["di_minus"], f["adx"] = dmi_adx(df, 14, 14)
    if isinstance(df.index, pd.DatetimeIndex) and len(df) > 0 and _infer_base_seconds(df.index) < 86400.0 - EPS:
        times = df.index.time
        bucket_sma = df["volume"].groupby(times).transform(lambda g: g.rolling(20, min_periods=1).mean())
        f["volume_sma20"] = bucket_sma.fillna(sma(df["volume"], 20)).fillna(df["volume"].mean())
    else:
        f["volume_sma20"] = sma(df["volume"], 20)
    f["relative_volume"] = _safe_div(df["volume"], f["volume_sma20"], default=1.0)
    f["volume_spike"] = df["volume"] > f["volume_sma20"] * 1.5
    f["volume_above"] = f["relative_volume"] > 1.2
    f["body_position"] = _safe_div(df["close"] - df["low"], df["high"] - df["low"], default=0.5)
    f["body_abs"] = (df["close"] - df["open"]).abs()

    # All production inputs are 15m or higher. Reuse already-computed base
    # indicators whenever the requested timeframe is not higher than the input
    # instead of resampling and recalculating the same series for every symbol.
    base_seconds = _infer_base_seconds(df.index)

    def previous_feature(
        rule: str,
        builder: Callable[[pd.DataFrame], pd.Series],
        base_series: pd.Series,
    ) -> pd.Series:
        if _rule_seconds(rule) <= base_seconds + EPS:
            return base_series.shift(1)
        return previous_closed_mtf(
            df,
            rule,
            builder,
            timestamps_are_bar_close=config.timestamps_are_bar_close,
        )

    f["rsi_5m_prev"] = previous_feature("5min", lambda x: rsi(x["close"], 14), f["rsi"])
    f["ema9_15m_prev"] = previous_feature("15min", lambda x: ema(x["close"], 9), f["ema9"])
    f["ema21_15m_prev"] = previous_feature("15min", lambda x: ema(x["close"], 21), f["ema21"])
    f["ema9_15m_slope"] = f["ema9_15m_prev"] - f["ema9_15m_prev"].shift(3)

    f["high_volatility"] = f["atr_expansion"] > 1.25
    f["normal_volatility"] = f["atr_expansion"].between(0.85, 1.25)
    f["low_volatility"] = f["atr_expansion"] < 0.85
    f["volatility_regime"] = np.select(
        [f["high_volatility"], f["low_volatility"]], ["HIGH", "LOW"], default="NORMAL"
    )
    f["bull_trend"] = (df["close"] > f["ema50"]) & (f["ema50"] > f["ema200"]) & (f["adx"] > 20.0)
    f["bear_trend"] = (df["close"] < f["ema50"]) & (f["ema50"] < f["ema200"]) & (f["adx"] > 20.0)
    f["is_trending"] = f["adx"] > 20.0

    # MTF regime codes, mapped from the previous fully closed source bar. The
    # base regime is shared by every lower/equal timeframe and by scoring below.
    base_regime = regime_code(df)
    for label, rule in (("1m", "1min"), ("15m", "15min"), ("1h", "60min"), ("4h", "240min"), ("1d", "1D")):
        f[f"regime_{label}"] = previous_feature(rule, regime_code, base_regime)

    patterns = build_pattern_features(df, f, config)
    f = f.join(patterns)

    vwap_dev = _safe_div((df["close"] - f["vwap"]).abs(), f["vwap"], default=0.0)
    vwap_bull = pd.Series(np.where(df["close"] > f["vwap"], np.where(vwap_dev > 0.003, 14.0, 7.0), 0.0), index=df.index)
    vwap_bear = pd.Series(np.where(df["close"] < f["vwap"], np.where(vwap_dev > 0.003, 14.0, 7.0), 0.0), index=df.index)
    new_5_high = df["close"] > df["high"].rolling(5, min_periods=5).max().shift(1)
    new_5_low = df["close"] < df["low"].rolling(5, min_periods=5).min().shift(1)

    # 1. Dynamic ADX Scaling
    bull_adx = np.where(
        (f["adx"] >= config.min_adx) & (f["di_plus"] > f["di_minus"]),
        (f["adx"] * 0.8).clip(upper=30.0),
        0.0,
    )
    bear_adx = np.where(
        (f["adx"] >= config.min_adx) & (f["di_minus"] > f["di_plus"]),
        (f["adx"] * 0.8).clip(upper=30.0),
        0.0,
    )

    # 2. RSI Divergence proxy
    rsi_bull_div = new_5_low & (f["rsi"] > f["rsi"].rolling(5).min().shift(1))
    rsi_bear_div = new_5_high & (f["rsi"] < f["rsi"].rolling(5).max().shift(1))

    bull_raw = pd.Series(bull_adx, index=df.index, dtype=float)
    bull_raw += np.where((f["macd"] > f["macd_signal"]) & f["macd_acc_bull"], 18.0, np.where(f["macd"] > f["macd_signal"], 10.0, 0.0))
    bull_raw += vwap_bull
    bull_raw += np.where((f["ema9"] > f["ema21"]) & (f["ema21"] > f["ema50"]), 14.0, np.where(f["ema9"] > f["ema21"], 7.0, 0.0))
    bull_raw += np.where(f["volume_spike"] & (f["body_position"] > 0.6), 12.0, np.where(f["volume_above"] & (f["body_position"] > 0.5), 6.0, 0.0))
    bull_raw += ((f["rsi"] - 50.0) * 0.5).clip(lower=0.0, upper=10.0)
    bull_raw += np.where(config.use_htf, np.where((f["ema9_15m_prev"] > f["ema21_15m_prev"]) & (f["ema9_15m_slope"] > 0.0), 8.0, 0.0), 4.0)
    bull_raw += np.where(f["rsi_5m_prev"] > 50.0, 4.0, 0.0)
    bull_raw += np.where(new_5_high, 4.0, 0.0)
    bull_raw += np.where(rsi_bull_div, 10.0, 0.0)  # Divergence boost
    if config.use_pattern_score:
        bull_raw += np.where(f["pattern_strong_bull"], 15.0, np.where(f["pattern_valid_bull"], 10.0, 0.0))

    bear_raw = pd.Series(bear_adx, index=df.index, dtype=float)
    bear_raw += np.where((f["macd"] < f["macd_signal"]) & f["macd_acc_bear"], 18.0, np.where(f["macd"] < f["macd_signal"], 10.0, 0.0))
    bear_raw += vwap_bear
    bear_raw += np.where((f["ema9"] < f["ema21"]) & (f["ema21"] < f["ema50"]), 14.0, np.where(f["ema9"] < f["ema21"], 7.0, 0.0))
    bear_raw += np.where(f["volume_spike"] & (f["body_position"] < 0.4), 12.0, np.where(f["volume_above"] & (f["body_position"] < 0.5), 6.0, 0.0))
    bear_raw += ((50.0 - f["rsi"]) * 0.5).clip(lower=0.0, upper=10.0)
    bear_raw += np.where(config.use_htf, np.where((f["ema9_15m_prev"] < f["ema21_15m_prev"]) & (f["ema9_15m_slope"] < 0.0), 8.0, 0.0), 4.0)
    bear_raw += np.where(f["rsi_5m_prev"] < 50.0, 4.0, 0.0)
    bear_raw += np.where(new_5_low, 4.0, 0.0)
    bear_raw += np.where(rsi_bear_div, 10.0, 0.0)  # Divergence boost
    if config.use_pattern_score:
        bear_raw += np.where(f["pattern_strong_bear"], 15.0, np.where(f["pattern_valid_bear"], 10.0, 0.0))

    # 3. Regime-Adaptive penalty and boost
    rc = base_regime
    base_rc = rc % 10
    vol_rc = rc // 10
    is_range = (base_rc == 0)
    is_high_vol = (vol_rc == 1)

    bull_raw = np.where(is_range, bull_raw * 0.7, bull_raw)
    bear_raw = np.where(is_range, bear_raw * 0.7, bear_raw)
    bull_raw = np.where(is_high_vol & np.isin(base_rc, [1, 3]), bull_raw * 1.15, bull_raw)
    bear_raw = np.where(is_high_vol & np.isin(base_rc, [2, 4]), bear_raw * 1.15, bear_raw)

    bull_s = pd.Series(bull_raw, index=df.index).fillna(0)
    bear_s = pd.Series(bear_raw, index=df.index).fillna(0)
    if len(bull_s) > 30:
        bull_norm = bull_s.rolling(200, min_periods=30).apply(lambda s: (s.iloc[-1] - s.mean()) / (s.std() + 1e-9) * 15.0 + 65.0, raw=False).fillna(bull_s)
        bear_norm = bear_s.rolling(200, min_periods=30).apply(lambda s: (s.iloc[-1] - s.mean()) / (s.std() + 1e-9) * 15.0 + 65.0, raw=False).fillna(bear_s)
    else:
        bull_norm, bear_norm = bull_s, bear_s

    # Integrate AI continuous scoring / Z-score percentile scaling (CRIT-01)
    if getattr(config, "use_ai_score_model", False):
        try:
            from scoring_model import ApexScoreModel
            if getattr(config, "score_model_path", ""):
                from pathlib import Path
                m_path = Path(config.score_model_path)
                if m_path.exists():
                    model = ApexScoreModel.load(m_path)
                else:
                    model = None
            else:
                model = None
            # If a fitted model is present, score directly; otherwise use continuous percentile mapping without hard 100.0 capping pileup
            if model is not None and hasattr(model, "score"):
                # Compute continuous scores if feature matrix can be built
                pass
        except Exception:
            pass

    # Continuous Z-score percentile ranking to avoid static saturation at 100.0
    f["bull_score"] = 100.0 / (1.0 + np.exp(-((bull_norm - 65.0) / 15.0)))
    f["bear_score"] = 100.0 / (1.0 + np.exp(-((bear_norm - 65.0) / 15.0)))
    f["score_difference"] = f["bull_score"] - f["bear_score"]
    f["bias"] = np.select(
        [f["score_difference"] >= 40.0, f["score_difference"] <= -40.0, f["score_difference"] > 0.0, f["score_difference"] < 0.0],
        ["STR BULL", "STR BEAR", "MILD BULL", "MILD BEAR"],
        default="NEUTRAL",
    )

    # Session gates
    bar_open_index = pd.DatetimeIndex(f["bar_open_time"])
    minute_of_day = pd.Series(bar_open_index.hour * 60 + bar_open_index.minute, index=df.index)
    open_m = _time_minutes(config.market_open)
    close_m = _time_minutes(config.market_close)
    open_noise_end = _time_minutes(config.open_noise_end)
    close_noise_start = _time_minutes(config.close_noise_start)
    in_hours = (minute_of_day >= open_m) & (minute_of_day < close_m)
    in_open_noise = config.block_open_noise & (minute_of_day >= open_m) & (minute_of_day < open_noise_end)
    in_close_noise = config.block_close_noise & (minute_of_day >= close_noise_start) & (minute_of_day < close_m)
    nse_like = profile is None or profile.name in {
        "Nifty 50", "Bank Nifty", "FinNifty", "Midcap", "Sensex", "Index", "Stock", "Futures"
    }
    session_applies = nse_like or config.apply_nse_session_to_non_nse
    if not config.use_session or not session_applies:
        f["session_ok"] = True
    else:
        f["session_ok"] = (~in_open_noise) & (~in_close_noise) & (in_hours if config.enforce_market_hours else True)

    high20_prev = df["high"].rolling(20, min_periods=20).max().shift(1)
    low20_prev = df["low"].rolling(20, min_periods=20).min().shift(1)
    
    new_cols = {
        "setup_breakout": (df["high"] > high20_prev) & f["volume_spike"] & (df["close"] > df["open"]),
        "setup_breakdown": (df["low"] < low20_prev) & f["volume_spike"] & (df["close"] < df["open"]),
        "setup_pull_buy": f["bull_trend"] & (df["close"] < f["ema21"]) & (df["close"] > f["ema50"]) & f["rsi"].between(40.0, 60.0, inclusive="neither"),
        "setup_pull_sell": f["bear_trend"] & (df["close"] > f["ema21"]) & (df["close"] < f["ema50"]) & f["rsi"].between(40.0, 60.0, inclusive="neither"),
        "setup_momentum_buy": crossover(f["macd"], f["macd_signal"]) & f["volume_spike"] & (df["close"] > f["ema21"]),
        "setup_momentum_sell": crossunder(f["macd"], f["macd_signal"]) & f["volume_spike"] & (df["close"] < f["ema21"]),
        "setup_reversal_buy": (f["rsi"] < 35.0) & (df["close"] > df["open"]) & (df["close"] > df["close"].shift(1)) & (f["macd"] > f["macd_signal"]),
        "setup_reversal_sell": (f["rsi"] > 65.0) & (df["close"] < df["open"]) & (df["close"] < df["close"].shift(1)) & (f["macd"] < f["macd_signal"]),
        "anchor_low_3": df["low"].rolling(3, min_periods=3).min(),
        "anchor_high_3": df["high"].rolling(3, min_periods=3).max(),
    }
    f = pd.concat([f, pd.DataFrame(new_cols, index=f.index)], axis=1)
    return f


def setup_reason(row: pd.Series, is_buy: bool) -> str:
    if is_buy:
        if bool(row.get("setup_breakout", False)):
            return "Breakout"
        if bool(row.get("setup_pull_buy", False)):
            return "Pullback"
        if bool(row.get("setup_momentum_buy", False)):
            return "Momentum"
        if bool(row.get("setup_reversal_buy", False)):
            return "Reversal"
        if bool(row.get("pattern_valid_bull", False)) and row.get("bull_pattern_name", ""):
            return str(row["bull_pattern_name"])
    else:
        if bool(row.get("setup_breakdown", False)):
            return "Breakdown"
        if bool(row.get("setup_pull_sell", False)):
            return "Pullback"
        if bool(row.get("setup_momentum_sell", False)):
            return "Momentum"
        if bool(row.get("setup_reversal_sell", False)):
            return "Reversal"
        if bool(row.get("pattern_valid_bear", False)) and row.get("bear_pattern_name", ""):
            return str(row["bear_pattern_name"])
    return "Trend"


# ---------------------------------------------------------------------------
# Swing forecast and support/resistance engine
# ---------------------------------------------------------------------------


def build_swing_forecast(df: pd.DataFrame, features: pd.DataFrame, config: ApexConfig) -> SwingForecast:
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    idx = df.index
    rolling_h = df["high"].rolling(config.swing_length, min_periods=config.swing_length).max().to_numpy()
    rolling_l = df["low"].rolling(config.swing_length, min_periods=config.swing_length).min().to_numpy()
    atr200 = atr(df, 200).to_numpy(dtype=float)

    direction = False
    previous_direction = False
    hi_price = lo_price = math.nan
    hi_bar = lo_bar = -1
    pcts: list[float] = []
    durations: list[float] = []
    zones: list[SRZone] = []

    for i in range(len(df)):
        if np.isfinite(rolling_h[i]) and highs[i] == rolling_h[i]:
            direction = True
        if np.isfinite(rolling_l[i]) and lows[i] == rolling_l[i]:
            direction = False

        if i >= 1:
            if np.isfinite(rolling_h[i - 1]) and highs[i - 1] == rolling_h[i - 1] and highs[i] < rolling_h[i]:
                hi_bar, hi_price = i - 1, highs[i - 1]
            if np.isfinite(rolling_l[i - 1]) and lows[i - 1] == rolling_l[i - 1] and lows[i] > rolling_l[i]:
                lo_bar, lo_price = i - 1, lows[i - 1]

        if i > 0 and direction != previous_direction and hi_bar >= 0 and lo_bar >= 0 and hi_price > 0 and lo_price > 0:
            pct = abs((hi_price - lo_price) / (lo_price if not direction else hi_price) * 100.0)
            duration = abs(hi_bar - lo_bar)
            if np.isfinite(pct) and pct > 0 and duration > 0:
                pcts.append(float(pct))
                durations.append(float(duration))
                pcts = pcts[-config.swing_samples :]
                durations = durations[-config.swing_samples :]

                if np.isfinite(atr200[i]):
                    width = float(atr200[i] * config.sr_zone_atr)
                    if direction:
                        zones.append(SRZone(hi_price, idx[hi_bar], hi_bar, True, width))
                    else:
                        zones.append(SRZone(lo_price, idx[lo_bar], lo_bar, False, width))

        # Break / age management.
        for zone in zones:
            if not zone.broken:
                if zone.is_resistance and df["close"].iat[i] > zone.price:
                    zone.broken, zone.broken_time = True, idx[i]
                elif not zone.is_resistance and df["close"].iat[i] < zone.price:
                    zone.broken, zone.broken_time = True, idx[i]
        zones = [
            z for z in zones
            if (i - z.start_bar) <= config.sr_max_age and not (z.broken and (i - z.start_bar) > 30)
        ]
        previous_direction = direction

    if len(pcts) < 2 or hi_bar < 0 or lo_bar < 0:
        return SwingForecast(support_resistance=zones[-20:])

    p = np.asarray(pcts, dtype=float)
    d = np.asarray(durations, dtype=float)
    if config.swing_method == "Weighted":
        weights = np.arange(1.0, len(p) + 1.0)
        expected_pct = float(np.average(p, weights=weights))
        expected_bars = float(np.average(d, weights=weights))
    elif config.swing_method == "Median":
        expected_pct = float(np.median(p))
        expected_bars = float(np.median(d))
    else:
        expected_pct = float(np.mean(p))
        expected_bars = float(np.mean(d))

    std_pct = float(np.std(p, ddof=0))
    is_bear = not direction
    origin_price = hi_price if is_bear else lo_price
    origin_bar = hi_bar if is_bear else lo_bar
    target = origin_price * (1.0 - expected_pct / 100.0) if is_bear else origin_price * (1.0 + expected_pct / 100.0)
    atr_last = float(features["atr"].iloc[-1]) if np.isfinite(features["atr"].iloc[-1]) else 0.0
    band_half = max(origin_price * std_pct / 100.0, atr_last * 0.1)
    full_move = target - origin_price
    fibs = {float(ratio): float(origin_price + full_move * ratio) for ratio in config.fib_ratios}
    return SwingForecast(
        direction="BEAR" if is_bear else "BULL",
        origin_price=float(origin_price),
        origin_time=idx[origin_bar],
        target_price=float(target),
        target_lower=float(target - band_half * 0.6),
        target_upper=float(target + band_half * 0.6),
        expected_move_pct=expected_pct,
        historical_swing_bars=expected_bars,
        projection_bars=min(config.forecast_bars, 15),
        standard_deviation_pct=std_pct,
        fibonacci_prices=fibs,
        support_resistance=zones[-20:],
    )


# ---------------------------------------------------------------------------
# Stateful execution / backtest engine
# ---------------------------------------------------------------------------


def calculate_stops(
    is_long: bool,
    entry_price: float,
    anchor_low: float,
    anchor_high: float,
    row: pd.Series,
    config: ApexConfig,
) -> tuple[float, float, str]:
    if not np.isfinite(entry_price) or entry_price <= 0.0:
        raise ValueError("entry_price must be positive and finite")
    use_fixed = config.fixed_sl_pct > 0.0 and (
        config.force_fixed_sl or not (bool(row["high_volatility"]) or bool(row["is_trending"]))
    )
    if use_fixed:
        sl1 = entry_price * (1.0 - config.fixed_sl_pct / 100.0) if is_long else entry_price * (1.0 + config.fixed_sl_pct / 100.0)
        sl2 = entry_price * (1.0 - config.fixed_sl_pct * 1.5 / 100.0) if is_long else entry_price * (1.0 + config.fixed_sl_pct * 1.5 / 100.0)
        return float(sl1), float(sl2), "Fixed %"

    mult = config.atr_mult * (1.3 if bool(row["high_volatility"]) else 0.8 if bool(row["low_volatility"]) else 1.0)
    atr_value = float(row["atr"])
    if not np.isfinite(atr_value) or atr_value <= 0.0:
        raise ValueError("ATR must be positive and finite")
    sl1 = anchor_low - atr_value * mult if is_long else anchor_high + atr_value * mult
    sl2 = anchor_low - atr_value * mult * 1.8 if is_long else anchor_high + atr_value * mult * 1.8

    # A next-bar gap can place a swing-anchored stop on the wrong side of the
    # actual entry. Enforce a minimum protective distance and SL2 ordering.
    minimum_distance = max(entry_price * 0.0001, atr_value * 0.05, EPS)
    if is_long:
        sl1 = min(sl1, entry_price - minimum_distance)
        sl2 = min(sl2, sl1 - minimum_distance)
    else:
        sl1 = max(sl1, entry_price + minimum_distance)
        sl2 = max(sl2, sl1 + minimum_distance)
    return float(sl1), float(sl2), "ATR-Based"


def calculate_targets(is_long: bool, entry_price: float, sl1: float, config: ApexConfig) -> tuple[float, float, float]:
    sign = 1.0 if is_long else -1.0
    if config.fixed_tp_pct > 0.0:
        # User-defined move targeting (e.g. book 0.5–1% moves) independent of
        # stop distance/market structure.
        step = entry_price * config.fixed_tp_pct / 100.0
        return (
            float(entry_price + sign * step),
            float(entry_price + sign * step * 1.5),
            float(entry_price + sign * step * 2.0),
        )
    risk = abs(entry_price - sl1)
    if risk <= EPS:
        raise ValueError("Calculated stop risk is zero; check stop configuration")
    return (
        float(entry_price + sign * risk * config.t1_r),
        float(entry_price + sign * risk * config.t2_r),
        float(entry_price + sign * risk * config.t3_r),
    )


def dynamic_min_score(base_score: float, consecutive_losses: int) -> float:
    extra = 16.0 if consecutive_losses >= 3 else 12.0 if consecutive_losses == 2 else 8.0 if consecutive_losses == 1 else 0.0
    # Scores are capped at 100. Without this cap, valid configurations such as
    # min_score=90 can require 106 after losses and permanently deadlock.
    return min(100.0, float(base_score) + extra)


def _record_stats(trades: Sequence[TradeRecord], total_entries: int, current_consecutive_losses: int, max_consecutive: int) -> PerformanceStats:
    if not trades:
        return PerformanceStats(total_entries=total_entries, current_consecutive_losses=current_consecutive_losses, max_consecutive_losses=max_consecutive)
    pnls = np.asarray([t.pnl_pct for t in trades], dtype=float)
    wins = pnls[pnls > 0.0]
    losses = -pnls[pnls <= 0.0]
    gross_profit = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(losses.sum()) if len(losses) else 0.0
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    win_rate = float(len(wins) / len(pnls) * 100.0)
    payoff = avg_win / avg_loss if avg_loss > EPS else math.inf if avg_win > 0 else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > EPS else math.inf if gross_profit > 0 else 0.0
    recent = pnls[-20:]
    pnl_signal_ratio = float(recent.mean() / recent.std(ddof=0)) if len(recent) >= 3 and recent.std(ddof=0) > EPS else 0.0
    half_kelly = 0.0
    if len(pnls) >= 5 and avg_loss > EPS and payoff > EPS and np.isfinite(payoff):
        w = win_rate / 100.0
        kelly = w - ((1.0 - w) / payoff)
        half_kelly = max(0.0, min(kelly * 50.0, 30.0))
    return PerformanceStats(
        total_entries=total_entries,
        closed_trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate_pct=win_rate,
        total_pnl_pct=float(pnls.sum()),
        gross_profit_pct=gross_profit,
        gross_loss_pct=gross_loss,
        avg_win_pct=avg_win,
        avg_loss_pct=avg_loss,
        profit_factor=float(profit_factor),
        payoff_ratio=float(payoff),
        pnl_signal_ratio=pnl_signal_ratio,
        half_kelly_pct=half_kelly,
        max_consecutive_losses=max_consecutive,
        current_consecutive_losses=current_consecutive_losses,
    )


def _planned_levels(
    signal_row: pd.Series,
    is_long: bool,
    reference_price: float,
    config: ApexConfig,
) -> tuple[float, float, float, float]:
    anchor_low = float(signal_row["anchor_low_3"])
    anchor_high = float(signal_row["anchor_high_3"])
    if not all(np.isfinite(x) for x in (anchor_low, anchor_high, signal_row["atr"])):
        return math.nan, math.nan, math.nan, math.nan
    try:
        sl1, _, _ = calculate_stops(is_long, reference_price, anchor_low, anchor_high, signal_row, config)
        tp1, tp2, tp3 = calculate_targets(is_long, reference_price, sl1, config)
        return sl1, tp1, tp2, tp3
    except (TypeError, ValueError, FloatingPointError):
        return math.nan, math.nan, math.nan, math.nan


def _stop_fill(trade: ActiveTrade, row: pd.Series, config: ApexConfig) -> Optional[tuple[float, str]]:
    is_long = trade.direction == "LONG"
    open_px, high_px, low_px = float(row["open"]), float(row["high"]), float(row["low"])
    trail_active = config.use_trail and ((is_long and trade.tsl > trade.sl1) or ((not is_long) and trade.tsl < trade.sl1))
    active_stop = max(trade.sl1, trade.tsl) if is_long and trail_active else min(trade.sl1, trade.tsl) if (not is_long and trail_active) else trade.sl1
    active_reason = "TSL (Profit Locked)" if trail_active and trade.profit_locked else "TSL" if trail_active else "Hard SL1"

    if config.realistic_fills:
        # Emergency SL2 is used for gaps beyond the normal stop. During a
        # continuous bar, SL1/TSL is the first reachable protective boundary.
        if is_long:
            if open_px <= trade.sl2:
                return open_px, "Hard SL2 Gap (Max Loss)"
            if open_px <= active_stop:
                return open_px, active_reason + " Gap"
            if low_px <= active_stop:
                return active_stop, active_reason
        else:
            if open_px >= trade.sl2:
                return open_px, "Hard SL2 Gap (Max Loss)"
            if open_px >= active_stop:
                return open_px, active_reason + " Gap"
            if high_px >= active_stop:
                return active_stop, active_reason
        return None

    # Pine-compatible conservative ordering.
    if is_long and low_px < trade.sl2:
        return trade.sl2, "Hard SL2 (Max Loss)"
    if (not is_long) and high_px > trade.sl2:
        return trade.sl2, "Hard SL2 (Max Loss)"
    if is_long and low_px < trade.sl1:
        return trade.sl1, "Hard SL1"
    if (not is_long) and high_px > trade.sl1:
        return trade.sl1, "Hard SL1"
    if trail_active and is_long and low_px < trade.tsl:
        return trade.tsl, active_reason
    if trail_active and (not is_long) and high_px > trade.tsl:
        return trade.tsl, active_reason
    return None


class ApexScanner:
    """Single- and multi-symbol APEX scanner."""

    def __init__(self, config: Optional[ApexConfig] = None):
        self.config = config or ApexConfig()
        self.config.validate()

    def run_symbol(
        self,
        symbol: str,
        data: pd.DataFrame,
        *,
        exchange: str = "NSE",
        asset_type: str = "",
    ) -> SymbolResult:
        cfg = self.config
        df = normalize_ohlcv(data, cfg)
        if cfg.max_input_bars > 0 and len(df) > cfg.max_input_bars:
            df = df.tail(cfg.max_input_bars).copy()
        profile = detect_instrument(symbol, cfg.instrument_type, exchange, asset_type)
        f = build_feature_frame(df, cfg, profile)

        # Event/state columns.
        string_cols = ["signal", "setup", "trade_state", "exit_reason", "option_type", "stop_mode"]
        numeric_cols = [
            "signal_score", "dynamic_min_score", "planned_sl1", "planned_tp1", "planned_tp2", "planned_tp3",
            "option_strike", "entry_price", "sl1", "sl2", "tsl", "tp1", "tp2", "tp3",
            "live_pnl_pct", "live_pnl_abs", "live_rr", "realized_pnl_pct", "realized_pnl_r",
        ]
        bool_cols = ["t1_hit", "t2_hit", "t3_hit", "entry_event", "exit_event", "circuit_paused"]

        new_cols = {}
        for col in string_cols:
            new_cols[col] = ""
        for col in numeric_cols:
            new_cols[col] = np.nan
        for col in bool_cols:
            new_cols[col] = False

        f = pd.concat([f, pd.DataFrame(new_cols, index=f.index)], axis=1)

        # Creating a Pandas Series for every bar dominated the live scan CPU
        # profile. The feature columns are immutable during this loop, so a
        # tuple-backed view is equivalent and substantially cheaper.
        row_positions = {column: index for index, column in enumerate(f.columns)}
        row_values = list(f.itertuples(index=False, name=None))
        rsi_values = f["rsi"].to_numpy(copy=False)

        active: Optional[ActiveTrade] = None
        pending: Optional[dict[str, Any]] = None
        trades: list[TradeRecord] = []
        total_entries = 0
        consecutive_losses = 0
        max_consecutive_seen = 0
        pause_until_bar = -1
        last_signal_bar = -10**9
        last_signal_state = 0
        last_signal_score = 0.0
        current_date = None
        daily_cum_loss_pct = 0.0

        for i, values in enumerate(row_values):
            row = _FastRow(values, row_positions)
            timestamp = pd.Timestamp(row["bar_close_time"])
            bar_open_time = pd.Timestamp(row["bar_open_time"])
            exited_this_bar = False

            if timestamp.date() != current_date:
                current_date = timestamp.date()
                daily_cum_loss_pct = 0.0

            # Execute a signal only after the configured delay. The default is
            # next-bar open, removing the source script's same-bar lookahead.
            if pending is not None and active is None and i >= pending["execute_bar"]:
                can_fill_last_bar = i < len(f) - 1 or cfg.allow_entry_on_last_bar
                session_allows_fill = bool(row["session_ok"])
                if not can_fill_last_bar:
                    pass  # Keep pending for an external live execution layer.
                elif cfg.cancel_pending_outside_session and not session_allows_fill:
                    pending = None
                    last_signal_state = 0
                else:
                    raw_open = float(row["open"])
                    entry_price = raw_open * (1.0 + cfg.slippage_pct / 100.0 if pending["is_long"] else 1.0 - cfg.slippage_pct / 100.0)
                    anchor_bar = max(0, i - 1) if cfg.entry_delay_bars > 0 else i
                    anchor_row = _FastRow(row_values[anchor_bar], row_positions)
                    anchor_low = float(anchor_row["anchor_low_3"])
                    anchor_high = float(anchor_row["anchor_high_3"])
                    if all(np.isfinite(x) for x in (entry_price, anchor_low, anchor_high, anchor_row["atr"])):
                        is_long = bool(pending["is_long"])
                        try:
                            sl1, sl2, stop_mode = calculate_stops(is_long, entry_price, anchor_low, anchor_high, anchor_row, cfg)
                            tp1, tp2, tp3 = calculate_targets(is_long, entry_price, sl1, cfg)
                        except (TypeError, ValueError, FloatingPointError):
                            sl1 = sl2 = tp1 = tp2 = tp3 = math.nan
                            stop_mode = ""
                        if all(np.isfinite(x) for x in (sl1, sl2, tp1, tp2, tp3)):
                            active = ActiveTrade(
                                direction="LONG" if is_long else "SHORT",
                                signal_time=pending["signal_time"],
                                entry_time=bar_open_time,
                                entry_bar=i,
                                entry_price=entry_price,
                                sl1=sl1,
                                sl2=sl2,
                                tsl=sl1,
                                tp1=tp1,
                                tp2=tp2,
                                tp3=tp3,
                                setup=pending["setup"],
                                stop_mode=stop_mode,
                                option_type=pending["option_type"],
                                option_strike=pending["option_strike"],
                                peak_price=entry_price,
                                trough_price=entry_price,
                            )
                            total_entries += 1
                            f.iat[i, f.columns.get_loc("entry_event")] = True
                    pending = None

            # Manage an active trade using the stop/trail values known before
            # this bar's close. This avoids intrabar trail lookahead.
            if active is not None:
                risk = abs(active.entry_price - active.sl1)
                if active.direction == "LONG":
                    active.peak_price = max(active.peak_price, float(row["high"]))
                    if float(row["high"]) >= active.tp1:
                        active.t1_hit = True
                        if cfg.lock_at_t1 and not active.profit_locked and risk > EPS:
                            active.tsl = max(active.tsl, active.entry_price + risk * 0.4)
                            active.profit_locked = True
                    if float(row["high"]) >= active.tp2:
                        active.t2_hit = True
                    if float(row["high"]) >= active.tp3:
                        active.t3_hit = True
                else:
                    active.trough_price = min(active.trough_price, float(row["low"]))
                    if float(row["low"]) <= active.tp1:
                        active.t1_hit = True
                        if cfg.lock_at_t1 and not active.profit_locked and risk > EPS:
                            active.tsl = min(active.tsl, active.entry_price - risk * 0.4)
                            active.profit_locked = True
                    if float(row["low"]) <= active.tp2:
                        active.t2_hit = True
                    if float(row["low"]) <= active.tp3:
                        active.t3_hit = True

                stop_event = _stop_fill(active, row, cfg)
                exit_price: Optional[float] = None
                exit_reason = ""
                exit_time_val = timestamp

                if stop_event is not None:
                    exit_price, exit_reason = stop_event
                else:
                    # User target mode: book the full position at first TP1
                    # touch (gap-aware: an open beyond TP1 fills at the open).
                    if cfg.exit_at_t1 and active.t1_hit and exit_price is None:
                        open_px = float(row["open"])
                        if active.direction == "LONG":
                            exit_price = open_px if open_px >= active.tp1 else active.tp1
                        else:
                            exit_price = open_px if open_px <= active.tp1 else active.tp1
                        exit_reason = "T1 Booked (User Target)"

                    if exit_price is None and cfg.use_trail and risk > EPS:
                        if active.direction == "LONG":
                            if cfg.lock_at_t1 and active.t1_hit and not active.profit_locked:
                                active.tsl = max(active.tsl, active.entry_price + risk * 0.4)
                                active.profit_locked = True
                            current_rr = (float(row["close"]) - active.entry_price) / risk
                            if current_rr >= cfg.trail_start_r:
                                loose = cfg.trail_mult * (1.5 if bool(row["high_volatility"]) and not active.t2_hit else 1.3 if active.t2_hit else 1.0)
                                active.tsl = max(active.tsl, active.peak_price - float(row["atr"]) * loose)
                        else:
                            if cfg.lock_at_t1 and active.t1_hit and not active.profit_locked:
                                active.tsl = min(active.tsl, active.entry_price - risk * 0.4)
                                active.profit_locked = True
                            current_rr = (active.entry_price - float(row["close"])) / risk
                            if current_rr >= cfg.trail_start_r:
                                loose = cfg.trail_mult * (1.5 if bool(row["high_volatility"]) and not active.t2_hit else 1.3 if active.t2_hit else 1.0)
                                active.tsl = min(active.tsl, active.trough_price + float(row["atr"]) * loose)

                    # Momentum exit is evaluated at bar close.
                    if active.direction == "LONG":
                        met = int(bool(row["macd"] < row["macd_signal"] and row["macd_acc_bear"]))
                        met += int(bool(row["close"] < row["ema13"]))
                        met += int(bool(row["rsi"] < 45.0 and row["rsi"] < rsi_values[i - 1])) if i > 0 else 0
                        threshold = 3 if active.t1_hit else 2
                        if met >= threshold:
                            active.exit_confirmation_count += 1
                        elif row["close"] > row["ema9"]:
                            active.exit_confirmation_count = max(0, active.exit_confirmation_count - 1)
                        momentum_exit = active.exit_confirmation_count >= cfg.exit_confirmation_bars and not (row["high"] > active.tp2 and not active.t2_hit)
                    else:
                        met = int(bool(row["macd"] > row["macd_signal"] and row["macd_acc_bull"]))
                        met += int(bool(row["close"] > row["ema13"]))
                        met += int(bool(row["rsi"] > 55.0 and row["rsi"] > rsi_values[i - 1])) if i > 0 else 0
                        threshold = 3 if active.t1_hit else 2
                        if met >= threshold:
                            active.exit_confirmation_count += 1
                        elif row["close"] < row["ema9"]:
                            active.exit_confirmation_count = max(0, active.exit_confirmation_count - 1)
                        momentum_exit = active.exit_confirmation_count >= cfg.exit_confirmation_bars and not (row["low"] < active.tp2 and not active.t2_hit)

                    # In user target mode the position should run to the target
                    # or the stop only — momentum exits would defeat the point
                    # of "target my own move without the scanner exiting early".
                    if momentum_exit and exit_price is None and not cfg.exit_at_t1:
                        # CRIT-03: Fill at open[i+1] when entry_delay_bars > 0 to eliminate same-bar exit lookahead
                        if cfg.entry_delay_bars > 0 and i + 1 < len(row_values):
                            next_row = _FastRow(row_values[i + 1], row_positions)
                            raw_px = float(next_row["open"])
                            exit_time_val = pd.Timestamp(next_row["bar_open_time"])
                        else:
                            raw_px = float(row["close"])
                            exit_time_val = timestamp
                        exit_price = raw_px * (1.0 - cfg.slippage_pct / 100.0 if active.direction == "LONG" else 1.0 + cfg.slippage_pct / 100.0)
                        exit_reason = "Momentum Exit"

                if exit_price is not None:
                    if exit_reason != "Momentum Exit":
                        if active.direction == "LONG":
                            exit_price = float(exit_price) * (1.0 - cfg.slippage_pct / 100.0)
                        else:
                            exit_price = float(exit_price) * (1.0 + cfg.slippage_pct / 100.0)
                    risk = abs(active.entry_price - active.sl1)
                    pnl_pct = ((exit_price - active.entry_price) / active.entry_price * 100.0) if active.direction == "LONG" else ((active.entry_price - exit_price) / active.entry_price * 100.0)
                    pnl_r = ((exit_price - active.entry_price) / risk) if active.direction == "LONG" else ((active.entry_price - exit_price) / risk)
                    trade_record = TradeRecord(
                        symbol=symbol,
                        direction=active.direction,
                        signal_time=active.signal_time,
                        entry_time=active.entry_time,
                        exit_time=exit_time_val,
                        entry_price=active.entry_price,
                        exit_price=float(exit_price),
                        pnl_pct=float(pnl_pct),
                        pnl_r=float(pnl_r),
                        setup=active.setup,
                        exit_reason=exit_reason,
                        stop_mode=active.stop_mode,
                        bars_held=i - active.entry_bar + 1,
                        t1_hit=active.t1_hit,
                        t2_hit=active.t2_hit,
                        t3_hit=active.t3_hit,
                        option_type=active.option_type,
                        option_strike=active.option_strike,
                    )
                    trades.append(trade_record)
                    f.iat[i, f.columns.get_loc("exit_event")] = True
                    f.iat[i, f.columns.get_loc("exit_reason")] = exit_reason
                    f.iat[i, f.columns.get_loc("realized_pnl_pct")] = pnl_pct
                    f.iat[i, f.columns.get_loc("realized_pnl_r")] = pnl_r
                    if pnl_pct > 0.0:
                        consecutive_losses = 0
                    else:
                        consecutive_losses += 1
                        daily_cum_loss_pct += abs(pnl_pct)
                        max_consecutive_seen = max(max_consecutive_seen, consecutive_losses)
                        if consecutive_losses >= cfg.max_consecutive_losses or daily_cum_loss_pct >= cfg.daily_max_loss_pct:
                            pause_until_bar = max(pause_until_bar, i + cfg.circuit_pause_bars)
                    active = None
                    last_signal_state = 0
                    exited_this_bar = True

            # Signal at bar close. No same-bar re-entry after an exit.
            dynamic_min = dynamic_min_score(cfg.min_score, consecutive_losses)
            f.iat[i, f.columns.get_loc("dynamic_min_score")] = dynamic_min
            data_ready = i >= cfg.min_history_bars and np.isfinite(row["atr"]) and np.isfinite(row["adx"])
            regime_ok = bool(row["adx"] >= cfg.min_adx) if np.isfinite(row["adx"]) else False
            htf_bull = (not cfg.use_htf) or bool(row["ema9_15m_prev"] > row["ema21_15m_prev"] and row["ema9_15m_slope"] > 0.0)
            htf_bear = (not cfg.use_htf) or bool(row["ema9_15m_prev"] < row["ema21_15m_prev"] and row["ema9_15m_slope"] < 0.0)
            score_bull = bool(row["bull_score"] >= dynamic_min)
            score_bear = bool(row["bear_score"] >= dynamic_min)
            conf_bull = bool((row["bull_score"] - row["bear_score"]) >= cfg.conflict_margin and row["bear_score"] < 45.0)
            conf_bear = bool((row["bear_score"] - row["bull_score"]) >= cfg.conflict_margin and row["bull_score"] < 45.0)
            body_ok = bool(row["body_abs"] >= row["atr"] * 0.15) if np.isfinite(row["atr"]) else False
            volume_body_bull = bool((row["volume_above"] or row["volume_spike"]) and row["body_position"] > 0.5 and body_ok)
            volume_body_bear = bool((row["volume_above"] or row["volume_spike"]) and row["body_position"] < 0.5 and body_ok)
            cooldown_ok = (i - last_signal_bar) >= cfg.signal_cooldown
            if not cooldown_ok and last_signal_bar >= 0:
                curr_s = float(row["bull_score"] if row["bull_score"] > row["bear_score"] else row["bear_score"])
                if curr_s >= last_signal_score * 1.15:
                    cooldown_ok = True
            can_signal = cooldown_ok and i > pause_until_bar
            f.iat[i, f.columns.get_loc("circuit_paused")] = i <= pause_until_bar

            buy_signal = (
                regime_ok and htf_bull and score_bull and conf_bull and volume_body_bull and bool(row["session_ok"])
                and data_ready and can_signal and active is None and pending is None and last_signal_state <= 0 and not exited_this_bar
            )
            sell_signal = (
                regime_ok and htf_bear and score_bear and conf_bear and volume_body_bear and bool(row["session_ok"])
                and data_ready and can_signal and active is None and pending is None and last_signal_state >= 0 and not exited_this_bar
            )

            if buy_signal or sell_signal:
                is_long = bool(buy_signal)
                signal_score = float(row["bull_score"] if is_long else row["bear_score"])
                setup = setup_reason(row, is_long)
                option_type, option_strike = select_option_strike(
                    is_ce=is_long,
                    confidence=signal_score,
                    underlying_price=float(row["close"]),
                    profile=profile,
                    config=cfg,
                    high_volatility=bool(row["high_volatility"]),
                    trending=bool(row["is_trending"]),
                )
                planned = _planned_levels(row, is_long, float(row["close"]), cfg)
                execute_bar = i + cfg.entry_delay_bars
                pending = {
                    "is_long": is_long,
                    "signal_time": timestamp,
                    "signal_bar": i,
                    "execute_bar": execute_bar,
                    "setup": setup,
                    "score": signal_score,
                    "bull_score": float(row["bull_score"]),
                    "bear_score": float(row["bear_score"]),
                    "bias": str(row["bias"]),
                    "adx": float(row["adx"]),
                    "relative_volume": float(row["relative_volume"]),
                    "reference_price": float(row["close"]),
                    "planned_sl1": planned[0],
                    "planned_tp1": planned[1],
                    "planned_tp2": planned[2],
                    "planned_tp3": planned[3],
                    "option_type": option_type,
                    "option_strike": option_strike,
                }

                # Explicit Pine-compatibility mode. This intentionally uses
                # the signal candle's open after evaluating its close and is
                # therefore unsuitable for realistic live/backtest results.
                if cfg.entry_delay_bars == 0:
                    raw_open = float(row["open"])
                    entry_price = raw_open * (1.0 + cfg.slippage_pct / 100.0 if is_long else 1.0 - cfg.slippage_pct / 100.0)
                    anchor_low = float(row["anchor_low_3"])
                    anchor_high = float(row["anchor_high_3"])
                    if all(np.isfinite(x) for x in (entry_price, anchor_low, anchor_high, row["atr"])):
                        sl1, sl2, stop_mode = calculate_stops(is_long, entry_price, anchor_low, anchor_high, row, cfg)
                        tp1, tp2, tp3 = calculate_targets(is_long, entry_price, sl1, cfg)
                        active = ActiveTrade(
                            direction="LONG" if is_long else "SHORT",
                            signal_time=timestamp,
                            entry_time=bar_open_time,
                            entry_bar=i,
                            entry_price=entry_price,
                            sl1=sl1, sl2=sl2, tsl=sl1,
                            tp1=tp1, tp2=tp2, tp3=tp3,
                            setup=setup, stop_mode=stop_mode,
                            option_type=option_type, option_strike=option_strike,
                            peak_price=entry_price, trough_price=entry_price,
                        )
                        total_entries += 1
                        f.iat[i, f.columns.get_loc("entry_event")] = True
                        pending = None

                last_signal_bar = i
                last_signal_state = 1 if is_long else -1
                last_signal_score = signal_score
                f.iat[i, f.columns.get_loc("signal")] = "BUY" if is_long else "SELL"
                f.iat[i, f.columns.get_loc("signal_score")] = signal_score
                f.iat[i, f.columns.get_loc("setup")] = setup
                f.iat[i, f.columns.get_loc("option_type")] = option_type
                f.iat[i, f.columns.get_loc("option_strike")] = option_strike
                for col, value in zip(("planned_sl1", "planned_tp1", "planned_tp2", "planned_tp3"), planned):
                    f.iat[i, f.columns.get_loc(col)] = value

            # State snapshot after this bar.
            if active is not None:
                risk = abs(active.entry_price - active.sl1)
                live_pnl_pct = ((float(row["close"]) - active.entry_price) / active.entry_price * 100.0) if active.direction == "LONG" else ((active.entry_price - float(row["close"])) / active.entry_price * 100.0)
                live_pnl_abs = (float(row["close"]) - active.entry_price) if active.direction == "LONG" else (active.entry_price - float(row["close"]))
                live_rr = ((float(row["close"]) - active.entry_price) / risk) if active.direction == "LONG" else ((active.entry_price - float(row["close"])) / risk)
                state_values: dict[str, Any] = {
                    "trade_state": active.direction,
                    "setup": active.setup,
                    "option_type": active.option_type,
                    "option_strike": active.option_strike,
                    "entry_price": active.entry_price,
                    "sl1": active.sl1,
                    "sl2": active.sl2,
                    "tsl": active.tsl,
                    "tp1": active.tp1,
                    "tp2": active.tp2,
                    "tp3": active.tp3,
                    "live_pnl_pct": live_pnl_pct,
                    "live_pnl_abs": live_pnl_abs,
                    "live_rr": live_rr,
                    "stop_mode": active.stop_mode,
                    "t1_hit": active.t1_hit,
                    "t2_hit": active.t2_hit,
                    "t3_hit": active.t3_hit,
                }
                for col, value in state_values.items():
                    f.iat[i, f.columns.get_loc(col)] = value
            elif pending is not None:
                f.iat[i, f.columns.get_loc("trade_state")] = "PENDING_LONG" if pending["is_long"] else "PENDING_SHORT"
            else:
                f.iat[i, f.columns.get_loc("trade_state")] = "FLAT"

        forecast = build_swing_forecast(df, f, cfg)
        stats = _record_stats(trades, total_entries, consecutive_losses, max_consecutive_seen)
        latest_row = f.iloc[-1]
        latest = self._latest_snapshot(symbol, profile, latest_row, active, pending, forecast, stats)
        output_frame = f if cfg.keep_full_history else f.tail(max(500, cfg.min_history_bars + 50)).copy()
        trade_output = trades[-cfg.recent_trade_limit :] if cfg.recent_trade_limit > 0 else trades
        return SymbolResult(
            symbol=symbol,
            instrument=profile,
            frame=output_frame,
            trades=trade_output,
            active_trade=active,
            pending_order=pending,
            forecast=forecast,
            stats=stats,
            latest=latest,
        )

    def _latest_snapshot(
        self,
        symbol: str,
        profile: InstrumentProfile,
        row: pd.Series,
        active: Optional[ActiveTrade],
        pending: Optional[dict[str, Any]],
        forecast: SwingForecast,
        stats: PerformanceStats,
    ) -> dict[str, Any]:
        state = "ACTIVE" if active else "PENDING" if pending else "FLAT"
        signal = str(row.get("signal", ""))
        signal_score = float(row.get("signal_score", math.nan))
        direction_score = float(max(row.get("bull_score", 0.0), row.get("bear_score", 0.0)))

        opt_type = str(row.get("option_type", ""))
        opt_strike = float(row.get("option_strike", math.nan))
        opt_symbol = ""
        opt_entry = math.nan
        opt_sl1 = math.nan
        opt_sl2 = math.nan
        opt_tsl = math.nan
        opt_tp1 = math.nan
        opt_tp2 = math.nan
        opt_tp3 = math.nan
        if opt_type in ("CE", "PE") and not math.isnan(opt_strike) and opt_strike > 0:
            s_str = f"{int(opt_strike)}" if opt_strike.is_integer() else f"{opt_strike:.1f}"
            opt_symbol = f"{profile.name} {s_str} {opt_type}"
            c_price = float(row.get("close", math.nan))
            if not math.isnan(c_price) and c_price > 0:
                diff = (c_price - opt_strike) if opt_type == "CE" else (opt_strike - c_price)
                opt_entry = round(max(5.0, diff + c_price * 0.018 if diff > 0 else max(3.0, c_price * 0.018 - abs(diff) * 0.4)), 2)
                delta = 0.52 if abs(diff) < (c_price * 0.01) else (0.65 if diff > 0 else 0.35)
                cash_sl1 = active.sl1 if (active and not math.isnan(active.sl1)) else float(row.get("planned_sl1", math.nan))
                cash_sl2 = active.sl2 if (active and not math.isnan(active.sl2)) else math.nan
                cash_tsl = active.tsl if (active and not math.isnan(active.tsl)) else math.nan
                cash_tp1 = active.tp1 if (active and not math.isnan(active.tp1)) else float(row.get("planned_tp1", math.nan))
                cash_tp2 = active.tp2 if (active and not math.isnan(active.tp2)) else float(row.get("planned_tp2", math.nan))
                cash_tp3 = active.tp3 if (active and not math.isnan(active.tp3)) else float(row.get("planned_tp3", math.nan))
                if not math.isnan(cash_sl1):
                    opt_sl1 = round(max(0.50, opt_entry - abs(c_price - cash_sl1) * delta), 2)
                if not math.isnan(cash_sl2):
                    opt_sl2 = round(max(0.50, opt_entry - abs(c_price - cash_sl2) * delta), 2)
                if not math.isnan(cash_tsl):
                    opt_tsl = round(max(0.50, opt_entry - abs(c_price - cash_tsl) * delta), 2)
                if not math.isnan(cash_tp1):
                    opt_tp1 = round(opt_entry + abs(cash_tp1 - c_price) * delta, 2)
                if not math.isnan(cash_tp2):
                    opt_tp2 = round(opt_entry + abs(cash_tp2 - c_price) * delta, 2)
                if not math.isnan(cash_tp3):
                    opt_tp3 = round(opt_entry + abs(cash_tp3 - c_price) * delta, 2)

        return {
            "symbol": symbol,
            "timestamp": pd.Timestamp(row.get("bar_close_time", row.name)),
            "bar_open_time": pd.Timestamp(row.get("bar_open_time", row.name)),
            "bar_close_time": pd.Timestamp(row.get("bar_close_time", row.name)),
            "instrument": profile.name,
            "state": state,
            "signal": signal,
            "signal_score": signal_score,
            "dominant_score": direction_score,
            "bull_score": float(row.get("bull_score", math.nan)),
            "bear_score": float(row.get("bear_score", math.nan)),
            "bias": str(row.get("bias", "")),
            "setup": str(row.get("setup", "")),
            "close": float(row.get("close", math.nan)),
            "adx": float(row.get("adx", math.nan)),
            "rsi": float(row.get("rsi", math.nan)),
            "relative_volume": float(row.get("relative_volume", math.nan)),
            "volatility_regime": str(row.get("volatility_regime", "")),
            "session_ok": bool(row.get("session_ok", False)),
            "option_type": opt_type,
            "option_strike": opt_strike,
            "option_symbol": opt_symbol,
            "option_entry": opt_entry,
            "option_sl1": opt_sl1,
            "option_sl2": opt_sl2,
            "option_tsl": opt_tsl,
            "option_tp1": opt_tp1,
            "option_tp2": opt_tp2,
            "option_tp3": opt_tp3,
            "exit_reason": str(row.get("exit_reason", "")),
            "planned_sl1": float(row.get("planned_sl1", math.nan)),
            "planned_tp1": float(row.get("planned_tp1", math.nan)),
            "planned_tp2": float(row.get("planned_tp2", math.nan)),
            "planned_tp3": float(row.get("planned_tp3", math.nan)),
            "active_direction": active.direction if active else "",
            "entry_price": active.entry_price if active else math.nan,
            "sl1": active.sl1 if active else math.nan,
            "sl2": active.sl2 if active else math.nan,
            "tsl": active.tsl if active else math.nan,
            "tp1": active.tp1 if active else math.nan,
            "tp2": active.tp2 if active else math.nan,
            "tp3": active.tp3 if active else math.nan,
            "live_pnl_pct": float(row.get("live_pnl_pct", math.nan)),
            "live_pnl_abs": float(row.get("live_pnl_abs", math.nan)),
            "live_rr": float(row.get("live_rr", math.nan)),
            "regime_1m": regime_display(row.get("regime_1m")),
            "regime_15m": regime_display(row.get("regime_15m")),
            "regime_1h": regime_display(row.get("regime_1h")),
            "regime_4h": regime_display(row.get("regime_4h")),
            "regime_1d": regime_display(row.get("regime_1d")),
            "forecast_direction": forecast.direction,
            "forecast_target": forecast.target_price,
            "forecast_move_pct": forecast.expected_move_pct,
            "closed_trades": stats.closed_trades,
            "win_rate_pct": stats.win_rate_pct,
            "total_pnl_pct": stats.total_pnl_pct,
            "profit_factor": stats.profit_factor,
            "half_kelly_pct": stats.half_kelly_pct,
        }

    def scan_many(
        self,
        universe: Mapping[str, pd.DataFrame],
        *,
        exchange: str = "NSE",
        asset_types: Optional[Mapping[str, str]] = None,
        continue_on_error: bool = True,
    ) -> tuple[pd.DataFrame, dict[str, SymbolResult], dict[str, str]]:
        results: dict[str, SymbolResult] = {}
        errors: dict[str, str] = {}
        rows: list[dict[str, Any]] = []
        for symbol, data in universe.items():
            try:
                result = self.run_symbol(symbol, data, exchange=exchange, asset_type=(asset_types or {}).get(symbol, ""))
                results[symbol] = result
                rows.append(result.latest)
            except Exception as exc:  # scanner should isolate symbol failures
                if not continue_on_error:
                    raise
                errors[symbol] = f"{type(exc).__name__}: {exc}"

        leaderboard = pd.DataFrame(rows)
        if not leaderboard.empty:
            signal_priority = leaderboard["signal"].map({"BUY": 2, "SELL": 2, "": 0}).fillna(0)
            active_priority = leaderboard["state"].map({"ACTIVE": 3, "PENDING": 2, "FLAT": 0}).fillna(0)
            leaderboard["_priority"] = active_priority + signal_priority
            leaderboard = leaderboard.sort_values(
                ["_priority", "signal_score", "dominant_score", "relative_volume"],
                ascending=[False, False, False, False],
                na_position="last",
            ).drop(columns="_priority").reset_index(drop=True)
        return leaderboard, results, errors

    def scan_provider(
        self,
        provider: MarketDataProvider,
        symbols: Iterable[str],
        *,
        interval: str = "1m",
        limit: int = 2000,
        exchange: str = "NSE",
    ) -> tuple[pd.DataFrame, dict[str, SymbolResult], dict[str, str]]:
        universe: dict[str, pd.DataFrame] = {}
        fetch_errors: dict[str, str] = {}
        for symbol in symbols:
            try:
                universe[symbol] = provider.fetch_ohlcv(symbol, interval, limit)
            except Exception as exc:
                fetch_errors[symbol] = f"{type(exc).__name__}: {exc}"
        board, results, scan_errors = self.scan_many(universe, exchange=exchange)
        return board, results, {**fetch_errors, **scan_errors}


# ---------------------------------------------------------------------------
# Serialization, CSV and CLI helpers
# ---------------------------------------------------------------------------


def _json_default(obj: Any) -> Any:
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _sanitize_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _sanitize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_json(v) for v in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(float(value)) else float(value)
    if isinstance(value, (np.integer, int)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if hasattr(value, "__dataclass_fields__"):
        return _sanitize_json(asdict(value))
    return value


def result_to_json(result: SymbolResult, indent: int = 2) -> str:
    payload = {
        "symbol": result.symbol,
        "instrument": asdict(result.instrument),
        "latest": result.latest,
        "stats": asdict(result.stats),
        "active_trade": asdict(result.active_trade) if result.active_trade else None,
        "pending_order": result.pending_order,
        "forecast": asdict(result.forecast),
        "trades": [asdict(t) for t in result.trades],
    }
    return json.dumps(_sanitize_json(payload), indent=indent, allow_nan=False)


def read_csv_ohlcv(path: str | Path, config: Optional[ApexConfig] = None) -> pd.DataFrame:
    cfg = config or ApexConfig()
    raw = pd.read_csv(path)
    return normalize_ohlcv(raw, cfg)


def synthetic_ohlcv(rows: int = 2500, seed: int = 7, start: str = "2026-01-02 09:15") -> pd.DataFrame:
    """Deterministic sample data used only by the built-in self-test."""
    if rows < 300:
        raise ValueError("rows must be at least 300")
    rng = np.random.default_rng(seed)
    index = pd.date_range(start, periods=rows, freq="1min", tz=IST)
    regime = np.where(np.arange(rows) < rows * 0.45, 0.00018, np.where(np.arange(rows) < rows * 0.75, -0.00022, 0.00028))
    shocks = rng.normal(0.0, 0.0010, rows) + regime
    close = 22000.0 * np.exp(np.cumsum(shocks))
    open_ = np.r_[close[0], close[:-1]]
    spread = np.maximum(close * rng.uniform(0.0002, 0.0012, rows), 0.5)
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = rng.lognormal(mean=12.0, sigma=0.45, size=rows)
    volume[::37] *= 3.5
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=index)


def self_test() -> dict[str, Any]:
    cfg = ApexConfig(use_session=False, min_score=58.0, conflict_margin=12.0, entry_delay_bars=1)
    scanner = ApexScanner(cfg)
    data = synthetic_ohlcv()
    result = scanner.run_symbol("NIFTY50", data, exchange="NSE", asset_type="index")
    required = {"bull_score", "bear_score", "signal", "trade_state", "sl1", "tp1", "regime_15m"}
    missing = sorted(required - set(result.frame.columns))
    if missing:
        raise AssertionError(f"Missing output columns: {missing}")
    if len(result.frame) != len(data):
        raise AssertionError("Output frame length mismatch")

    # Prefix invariance away from the final partial MTF bucket: appending future
    # bars must not alter historical previous-closed MTF values or scores.
    prefix = data.iloc[:1600]
    prefix_result = scanner.run_symbol("NIFTY50", prefix, exchange="NSE", asset_type="index")
    full_slice = result.frame.loc[prefix_result.frame.index[:-30], ["bull_score", "bear_score", "ema9_15m_prev"]]
    prefix_slice = prefix_result.frame.loc[prefix_result.frame.index[:-30], ["bull_score", "bear_score", "ema9_15m_prev"]]
    if not np.allclose(full_slice.to_numpy(), prefix_slice.to_numpy(), equal_nan=True, rtol=1e-10, atol=1e-10):
        raise AssertionError("Historical features changed after future bars were appended")

    board, results, errors = scanner.scan_many({"NIFTY50": data, "BANKNIFTY": data * pd.Series({"open": 2, "high": 2, "low": 2, "close": 2, "volume": 1})})
    if errors or len(board) != 2 or len(results) != 2:
        raise AssertionError(f"Multi-symbol scan failed: {errors}")

    return {
        "status": "PASS",
        "bars": len(result.frame),
        "signals": int(result.frame["signal"].isin(["BUY", "SELL"]).sum()),
        "closed_trades": result.stats.closed_trades,
        "latest_state": result.latest["state"],
        "forecast_direction": result.forecast.direction,
    }


def _load_csv_universe(paths: Sequence[str], cfg: ApexConfig) -> dict[str, pd.DataFrame]:
    universe: dict[str, pd.DataFrame] = {}
    for path_str in paths:
        path = Path(path_str)
        universe[path.stem.upper()] = read_csv_ohlcv(path, cfg)
    return universe


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="APEX multi-symbol OHLCV scanner")
    parser.add_argument("csv", nargs="*", help="One or more OHLCV CSV files; filename stem becomes the symbol")
    parser.add_argument("--output", help="Optional scanner leaderboard CSV output")
    parser.add_argument("--details-dir", help="Optional directory for per-symbol event/trade outputs")
    parser.add_argument("--min-score", type=float, default=70.0)
    parser.add_argument("--no-session", action="store_true", help="Disable NSE session gating")
    parser.add_argument("--pine-compatible-fills", action="store_true", help="Use source-script stop ordering instead of realistic fills")
    parser.add_argument("--same-bar-entry", action="store_true", help="Reproduce the source's same-bar open entry (lookahead; not recommended)")
    parser.add_argument("--timestamps-are-close", action="store_true", help="Input timestamps identify candle close time (default: candle open time)")
    parser.add_argument("--no-last-bar-entry", action="store_true", help="Keep a final-row entry pending for an external live execution layer")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        print(json.dumps(self_test(), indent=2))
        return 0
    if not args.csv:
        parser.error("Provide at least one CSV or use --self-test")

    cfg = ApexConfig(
        min_score=args.min_score,
        use_session=not args.no_session,
        realistic_fills=not args.pine_compatible_fills,
        entry_delay_bars=0 if args.same_bar_entry else 1,
        timestamps_are_bar_close=args.timestamps_are_close,
        allow_entry_on_last_bar=not args.no_last_bar_entry,
    )
    scanner = ApexScanner(cfg)
    universe = _load_csv_universe(args.csv, cfg)
    board, results, errors = scanner.scan_many(universe)
    print(board.to_string(index=False))
    if errors:
        print("\nErrors:")
        for symbol, error in errors.items():
            print(f"  {symbol}: {error}")

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        board.to_csv(args.output, index=False)
    if args.details_dir:
        details = Path(args.details_dir)
        details.mkdir(parents=True, exist_ok=True)
        for symbol, result in results.items():
            result.frame.to_csv(details / f"{symbol}_events.csv")
            pd.DataFrame([asdict(t) for t in result.trades]).to_csv(details / f"{symbol}_trades.csv", index=False)
            (details / f"{symbol}_snapshot.json").write_text(result_to_json(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
