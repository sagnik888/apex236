"""Shared simulation engine and execution utilities.

Unifies backtesting trade simulation across all research scripts (breakout_backtest,
edge_test, filter_research, score_rebuild) ensuring consistent 1-bar delay, realistic
slippage, transaction costs, and same-bar fill resolution. Also provides shared helpers
for execution mode inspection (PAPER vs LIVE).
"""
from __future__ import annotations

import logging
from typing import Any, Tuple, Optional
import numpy as np
import pandas as pd

from settings_store import get_settings

logger = logging.getLogger(__name__)

# Default round-trip cost (brokerage + STT + GST + stamp duty + exchange charges)
DEFAULT_ROUND_TRIP_COST_PCT = 0.182
DEFAULT_SLIPPAGE_PCT = 0.1


def get_execution_mode() -> str:
    """Return the current execution mode ('PAPER' or 'LIVE')."""
    settings = get_settings()
    mode = str(settings.get("execution_mode", "PAPER")).upper()
    return mode if mode in ("PAPER", "LIVE") else "PAPER"


def is_live_execution() -> bool:
    """Return True if the system is configured for live broker order placement."""
    return get_execution_mode() == "LIVE"


def simulate(
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    entry_idx: int,
    is_long: bool,
    tp_pct: float,
    sl_pct: float,
    max_bars: int = 200,
    cost_pct: float = DEFAULT_ROUND_TRIP_COST_PCT,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
    entry_delay_bars: int = 1,
    deduct_costs: bool = True,
) -> Optional[Tuple[float, int]]:
    """Simulate a trade fill with stop-loss and take-profit targets.

    Parameters:
        o, h, l, c: 1D numpy arrays for Open, High, Low, Close prices.
        entry_idx: Signal bar index (entry occurs at entry_idx + entry_delay_bars open).
        is_long: True for BUY/long, False for SELL/short.
        tp_pct: Take profit percentage from entry price.
        sl_pct: Stop loss percentage from entry price.
        max_bars: Maximum holding period in bars.
        cost_pct: Round-trip transaction charges percentage (applied if deduct_costs=True).
        slippage_pct: Slippage buffer applied on market entry and stop exits.
        entry_delay_bars: Number of bars after entry_idx to enter (default 1 = next bar open).
        deduct_costs: If True, subtract cost_pct from the returned percentage return.

    Returns:
        (return_pct, holding_bars) or None if entry is out of bounds or invalid.
    """
    if max_bars <= 0:
        return None
    exec_idx = entry_idx + entry_delay_bars
    if exec_idx >= len(o):
        return None

    raw_entry = float(o[exec_idx])
    if not np.isfinite(raw_entry) or raw_entry <= 0:
        return None

    sign = 1.0 if is_long else -1.0
    # Apply entry slippage
    entry = raw_entry * (1.0 + sign * (slippage_pct / 100.0))

    tp = entry * (1.0 + sign * (tp_pct / 100.0))
    sl = entry * (1.0 - sign * (sl_pct / 100.0))

    end_idx = min(exec_idx + max_bars, len(o))
    for i in range(exec_idx, end_idx):
        high_val = float(h[i])
        low_val = float(l[i])
        open_val = float(o[i])

        # Gap-through checks for all bars (C-03)
        if is_long:
            if open_val <= sl:
                ret = (open_val * (1.0 - slippage_pct / 100.0) - entry) / entry * 100.0
                return (ret - cost_pct if deduct_costs else ret), (i - entry_idx)
            if open_val >= tp:
                ret = (open_val - entry) / entry * 100.0
                return (ret - cost_pct if deduct_costs else ret), (i - entry_idx)
                
            hit_sl = low_val <= sl
            hit_tp = high_val >= tp
            if hit_sl and hit_tp:
                # Same-bar ambiguity: intrabar order is unknown, so resolve
                # conservatively to the STOP (matches _stop_fill and the live
                # run_symbol engine). Booking the TP here inflated backtest WR.
                ret = (sl * (1.0 - slippage_pct / 100.0) - entry) / entry * 100.0
                return (ret - cost_pct if deduct_costs else ret), (i - entry_idx)
            elif hit_sl:
                ret = (sl * (1.0 - slippage_pct / 100.0) - entry) / entry * 100.0
                return (ret - cost_pct if deduct_costs else ret), (i - entry_idx)
            elif hit_tp:
                ret = (tp - entry) / entry * 100.0
                return (ret - cost_pct if deduct_costs else ret), (i - entry_idx)
        else:
            if open_val >= sl:
                ret = (entry - open_val * (1.0 + slippage_pct / 100.0)) / entry * 100.0
                return (ret - cost_pct if deduct_costs else ret), (i - entry_idx)
            if open_val <= tp:
                ret = (entry - open_val) / entry * 100.0
                return (ret - cost_pct if deduct_costs else ret), (i - entry_idx)
                
            hit_sl = high_val >= sl
            hit_tp = low_val <= tp
            if hit_sl and hit_tp:
                # Conservative same-bar resolution: assume the STOP filled first.
                ret = (entry - sl * (1.0 + slippage_pct / 100.0)) / entry * 100.0
                return (ret - cost_pct if deduct_costs else ret), (i - entry_idx)
            elif hit_sl:
                ret = (entry - sl * (1.0 + slippage_pct / 100.0)) / entry * 100.0
                return (ret - cost_pct if deduct_costs else ret), (i - entry_idx)
            elif hit_tp:
                ret = (entry - tp) / entry * 100.0
                return (ret - cost_pct if deduct_costs else ret), (i - entry_idx)

    # Time-based exit at close of max_bars
    last_close = float(c[end_idx - 1]) * (1.0 - sign * (slippage_pct / 100.0))
    ret = sign * (last_close - entry) / entry * 100.0
    return (ret - cost_pct if deduct_costs else ret), (end_idx - 1 - entry_idx)


def simulate_exit(
    frame: pd.DataFrame,
    entry_idx: int,
    is_long: bool,
    tp_pct: float,
    sl_pct: float,
    max_bars: int = 200,
    cost_pct: float = DEFAULT_ROUND_TRIP_COST_PCT,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
    entry_delay_bars: int = 1,
    deduct_costs: bool = True,
) -> Optional[Tuple[float, int]]:
    """DataFrame convenience wrapper around simulate()."""
    if entry_idx < 0 or entry_idx >= len(frame):
        return None
    o = frame["open"].to_numpy()
    h = frame["high"].to_numpy()
    l = frame["low"].to_numpy()
    c = frame["close"].to_numpy()
    return simulate(
        o, h, l, c,
        entry_idx=entry_idx,
        is_long=is_long,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        max_bars=max_bars,
        cost_pct=cost_pct,
        slippage_pct=slippage_pct,
        entry_delay_bars=entry_delay_bars,
        deduct_costs=deduct_costs,
    )
