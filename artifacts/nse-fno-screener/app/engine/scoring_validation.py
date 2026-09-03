"""Offline walk-forward validation helpers for the scoring engine.

This module deliberately does not download data. Feed it closed OHLCV history from the
same source/timeframe used by the screener. It evaluates each historical signal only
against FUTURE bars, preventing lookahead contamination in reported hit rates.
"""
from __future__ import annotations

from dataclasses import asdict
import math
import pandas as pd

from app import indicators as ind
from app.engine.scoring import ScoreWeights, score_symbol_timeframe


def evaluate_signal_history(
    bars: pd.DataFrame,
    weights: ScoreWeights = ScoreWeights(),
    horizon_bars: int = 3,
    min_move_atr: float = 0.25,
    warmup_bars: int = 90,
    step: int = 1,
) -> dict:
    """Walk forward through history and measure directional precision.

    A bullish call is counted correct when the forward close-to-close move is at least
    +min_move_atr * ATR_at_signal; bearish is symmetric. Neutral rows are excluded from
    precision and included in coverage. This is a validation utility, not a trading P&L
    simulator: no fees, slippage, stops, gaps or intrabar execution assumptions are made.
    """
    if horizon_bars < 1 or step < 1:
        raise ValueError("horizon_bars and step must be >= 1")
    if len(bars) <= warmup_bars + horizon_bars:
        return {"status": "insufficient_data", "bars": len(bars)}

    atr14 = ind.atr(bars["high"], bars["low"], bars["close"], 14)
    bull_total = bull_hits = bear_total = bear_hits = neutral = 0
    bull_returns: list[float] = []
    bear_returns: list[float] = []
    observations = 0

    for i in range(warmup_bars, len(bars) - horizon_bars, step):
        history = bars.iloc[: i + 1]
        result = score_symbol_timeframe(history, weights=weights)
        if result.get("status") != "ok":
            continue
        observations += 1
        score = float(result["score"])
        now_close = float(bars["close"].iloc[i])
        future_close = float(bars["close"].iloc[i + horizon_bars])
        forward_return_pct = (future_close / now_close - 1.0) * 100.0
        atr_value = float(atr14.iloc[i]) if not pd.isna(atr14.iloc[i]) else 0.0
        move = future_close - now_close
        required = max(0.0, min_move_atr * atr_value)

        if score >= weights.bull_threshold:
            bull_total += 1
            bull_returns.append(forward_return_pct)
            if move >= required:
                bull_hits += 1
        elif score <= weights.bear_threshold:
            bear_total += 1
            bear_returns.append(forward_return_pct)
            if move <= -required:
                bear_hits += 1
        else:
            neutral += 1

    directional = bull_total + bear_total
    def pct(a: int, b: int):
        return round(100.0 * a / b, 2) if b else None
    def avg(xs: list[float]):
        return round(sum(xs) / len(xs), 4) if xs else None

    return {
        "status": "ok",
        "observations": observations,
        "horizon_bars": horizon_bars,
        "min_move_atr": min_move_atr,
        "bull_signals": bull_total,
        "bull_precision_pct": pct(bull_hits, bull_total),
        "bull_avg_forward_return_pct": avg(bull_returns),
        "bear_signals": bear_total,
        "bear_precision_pct": pct(bear_hits, bear_total),
        "bear_avg_forward_return_pct": avg(bear_returns),
        "directional_precision_pct": pct(bull_hits + bear_hits, directional),
        "signal_coverage_pct": pct(directional, observations),
        "neutral_observations": neutral,
        "weights": asdict(weights),
        "note": "Directional validation only; excludes costs, slippage, stops and intrabar execution.",
    }
