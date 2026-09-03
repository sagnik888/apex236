"""APEX Backtest API – run historical backtests against cached OHLCV data.

Provides POST /api/backtest and GET /api/backtest/data-range endpoints.
Uses the real ApexScanner + simulation_engine for honest next-bar fills,
slippage, gap-through resolution, and transaction costs.
"""
from __future__ import annotations

import logging
import math
import os
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
CACHE = HERE / "angel_cache"

logger = logging.getLogger("apex.backtest")


def _load_symbol(symbol: str, interval: str) -> pd.DataFrame | None:
    """Load cached OHLCV pickle for a single symbol."""
    clean = symbol.upper().replace(".NS", "")
    for fname in [f"{clean}__{interval}.pkl", f"{symbol}__{interval}.pkl"]:
        path = CACHE / fname
        if path.exists():
            try:
                df = pd.read_pickle(path)
                if isinstance(df, pd.DataFrame) and len(df) > 50:
                    return df
            except Exception:
                continue
    return None


def get_data_range() -> dict:
    """Return available date ranges for cached symbols."""
    intervals = {}
    for interval in ["15m", "1h"]:
        symbols = {}
        for path in sorted(CACHE.glob(f"*__{interval}.pkl")):
            try:
                df = pd.read_pickle(path)
                if isinstance(df, pd.DataFrame) and len(df) > 50:
                    sym = path.name.split("__")[0]
                    symbols[sym] = {
                        "start": str(df.index[0]),
                        "end": str(df.index[-1]),
                        "bars": len(df),
                    }
            except Exception:
                continue
        intervals[interval] = {
            "symbols": len(symbols),
            "range": symbols,
        }
    return intervals


def run_backtest(
    symbol: str | None = None,
    timeframe: str = "15m",
    days: int = 60,
    min_score: float = 60.0,
    conflict_margin: float = 10.0,
    atr_mult: float = 2.0,
    fixed_sl_pct: float = 0.0,
    fixed_tp_pct: float = 0.0,
    force_fixed_sl: bool = False,
    exit_at_t1: bool = False,
    slippage_pct: float = 0.05,
    cost_pct: float = 0.182,
    include_options: bool = False,
) -> dict:
    """Run a backtest against cached historical data.
    
    Args:
        symbol: Ticker (e.g. "RELIANCE") or None for all symbols.
        timeframe: "15m" or "1h".
        days: Number of days of history to backtest.
        min_score: Minimum signal score threshold.
        conflict_margin: Minimum bull-bear score gap.
        atr_mult: ATR multiplier for stop distance.
        fixed_sl_pct: Fixed stop loss percentage (0 = use ATR).
        fixed_tp_pct: Fixed take profit percentage (0 = use R-multiples).
        force_fixed_sl: Force fixed SL in all regimes.
        exit_at_t1: Exit full position at first target.
        slippage_pct: Per-side slippage percentage.
        cost_pct: Round-trip transaction cost percentage.
        include_options: Include synthetic option P&L estimation.
    
    Returns:
        Dictionary with backtest results.
    """
    from apex_python_scanner import ApexConfig, ApexScanner

    # Build config with user overrides
    cfg = ApexConfig(
        min_score=min_score,
        conflict_margin=conflict_margin,
        atr_mult=atr_mult,
        fixed_sl_pct=fixed_sl_pct,
        fixed_tp_pct=fixed_tp_pct,
        force_fixed_sl=force_fixed_sl,
        exit_at_t1=exit_at_t1,
        slippage_pct=slippage_pct,
        cost_pct=cost_pct,
        use_htf=True,
        entry_delay_bars=1,
        realistic_fills=True,
        use_session=True,
        enforce_market_hours=True,
        keep_full_history=True,
        max_input_bars=0,
        strict_ohlcv=False,
        min_history_bars=210,
        timeframe=timeframe,
    )

    # Wire ML model path
    model_path = HERE / "apex_score_model.json"
    if model_path.exists():
        cfg.score_model_path = str(model_path)

    scanner = ApexScanner(cfg)

    # Determine symbols to process
    if symbol:
        clean = symbol.upper().replace(".NS", "")
        symbols_to_run = [clean]
    else:
        symbols_to_run = sorted(
            set(p.name.split("__")[0] for p in CACHE.glob(f"*__{timeframe}.pkl"))
        )

    # Collect all trades
    all_trades: list[dict] = []
    symbol_summaries: list[dict] = []
    errors: list[str] = []

    for sym in symbols_to_run:
        df = _load_symbol(sym, timeframe)
        if df is None:
            errors.append(f"{sym}: no cached data")
            continue

        # Trim to requested days
        if days > 0 and len(df) > 0:
            cutoff = df.index[-1] - pd.Timedelta(days=days)
            # Keep some burn-in bars for indicators
            burn_in_cutoff = cutoff - pd.Timedelta(days=30)
            df_trimmed = df[df.index >= burn_in_cutoff]
            if len(df_trimmed) < 250:
                df_trimmed = df  # Use all data if not enough after trim
        else:
            df_trimmed = df

        try:
            result = scanner.run_symbol(sym, df_trimmed, asset_type="stock")
        except Exception as exc:
            errors.append(f"{sym}: {str(exc)[:100]}")
            continue

        frame = result.frame
        if frame.empty or "signal" not in frame.columns:
            continue

        # Trim results to the requested date range (exclude burn-in)
        if days > 0 and len(df) > 0:
            frame = frame[frame.index >= cutoff]

        # Extract completed trades from the frame
        sig_col = frame["signal"]
        state_col = frame.get("state", pd.Series("", index=frame.index))
        
        # Iterate through frame to find trade entries and exits
        in_trade = False
        trade_entry = None
        trade_bars = 0
        sym_trades = []

        for idx, row in frame.iterrows():
            sig = str(row.get("signal", ""))
            state = str(row.get("state", ""))
            exit_reason = str(row.get("exit_reason", ""))

            if not in_trade and sig in ("BUY", "SELL"):
                in_trade = True
                trade_entry = {
                    "symbol": sym,
                    "timeframe": timeframe,
                    "direction": sig,
                    "entry_time": str(idx),
                    "entry_price": float(row.get("entry_price", row.get("close", 0))),
                    "score": float(row.get("signal_score", 0)),
                    "setup": str(row.get("setup", "")),
                    "sl1": float(row.get("planned_sl1", row.get("sl1", 0))),
                    "tp1": float(row.get("planned_tp1", row.get("tp1", 0))),
                    "tp2": float(row.get("planned_tp2", row.get("tp2", 0))),
                    "tp3": float(row.get("planned_tp3", row.get("tp3", 0))),
                    "option_type": str(row.get("option_type", "")),
                    "option_strike": float(row.get("option_strike", 0)) if not math.isnan(float(row.get("option_strike", float("nan")))) else None,
                }
                trade_bars = 0

            if in_trade:
                trade_bars += 1

            if in_trade and exit_reason and exit_reason not in ("", "nan"):
                exit_price = float(row.get("close", 0))
                entry_price = trade_entry["entry_price"]
                is_long = trade_entry["direction"] == "BUY"

                if entry_price > 0:
                    gross_pnl = ((exit_price - entry_price) / entry_price * 100) * (1 if is_long else -1)
                    net_pnl = gross_pnl - cost_pct - (slippage_pct * 2)
                else:
                    gross_pnl = 0.0
                    net_pnl = 0.0

                trade_entry.update({
                    "exit_time": str(idx),
                    "exit_price": exit_price,
                    "exit_reason": exit_reason,
                    "gross_pnl_pct": round(gross_pnl, 4),
                    "net_pnl_pct": round(net_pnl, 4),
                    "hold_bars": trade_bars,
                    "won": net_pnl > 0,
                    "option_pnl_pct": float(row.get("option_pnl_pct", 0)) if include_options else None,
                })
                sym_trades.append(trade_entry)
                all_trades.append(trade_entry)
                in_trade = False
                trade_entry = None

        # Symbol summary
        if sym_trades:
            wins = sum(1 for t in sym_trades if t["won"])
            total = len(sym_trades)
            pnls = [t["net_pnl_pct"] for t in sym_trades]
            symbol_summaries.append({
                "symbol": sym,
                "total_trades": total,
                "wins": wins,
                "losses": total - wins,
                "win_rate_pct": round(wins / total * 100, 1) if total > 0 else 0,
                "total_pnl_pct": round(sum(pnls), 2),
                "avg_pnl_pct": round(np.mean(pnls), 4) if pnls else 0,
                "best_trade_pct": round(max(pnls), 2) if pnls else 0,
                "worst_trade_pct": round(min(pnls), 2) if pnls else 0,
            })

    # Compute portfolio-level metrics
    total_trades = len(all_trades)
    wins = sum(1 for t in all_trades if t["won"])
    losses = total_trades - wins
    pnls = [t["net_pnl_pct"] for t in all_trades]

    # Equity curve (cumulative P&L)
    equity_curve = []
    cumulative = 0.0
    for t in sorted(all_trades, key=lambda x: x.get("exit_time", "")):
        cumulative += t["net_pnl_pct"]
        equity_curve.append({
            "time": t.get("exit_time", ""),
            "equity": round(cumulative, 2),
            "symbol": t["symbol"],
            "pnl": t["net_pnl_pct"],
        })

    # Max drawdown
    peak = 0.0
    max_dd = 0.0
    for pt in equity_curve:
        peak = max(peak, pt["equity"])
        dd = peak - pt["equity"]
        max_dd = max(max_dd, dd)

    # Score band analysis
    score_bands = []
    band_edges = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 100)]
    for lo, hi in band_edges:
        band_trades = [t for t in all_trades if lo <= t.get("score", 0) < (hi + (1 if hi == 100 else 0))]
        if band_trades:
            bw = sum(1 for t in band_trades if t["won"])
            bp = [t["net_pnl_pct"] for t in band_trades]
            score_bands.append({
                "band": f"{lo}-{hi}",
                "count": len(band_trades),
                "win_rate_pct": round(bw / len(band_trades) * 100, 1),
                "avg_pnl_pct": round(np.mean(bp), 4),
                "total_pnl_pct": round(sum(bp), 2),
            })

    # Direction breakdown
    long_trades = [t for t in all_trades if t["direction"] == "BUY"]
    short_trades = [t for t in all_trades if t["direction"] == "SELL"]

    def _dir_stats(trades):
        if not trades:
            return {"count": 0, "win_rate_pct": 0, "avg_pnl_pct": 0, "total_pnl_pct": 0}
        w = sum(1 for t in trades if t["won"])
        p = [t["net_pnl_pct"] for t in trades]
        return {
            "count": len(trades),
            "win_rate_pct": round(w / len(trades) * 100, 1),
            "avg_pnl_pct": round(np.mean(p), 4),
            "total_pnl_pct": round(sum(p), 2),
        }

    # Setup breakdown
    setups = {}
    for t in all_trades:
        s = t.get("setup", "Unknown") or "Unknown"
        if s not in setups:
            setups[s] = []
        setups[s].append(t)
    setup_breakdown = []
    for s, trades in sorted(setups.items()):
        w = sum(1 for t in trades if t["won"])
        p = [t["net_pnl_pct"] for t in trades]
        setup_breakdown.append({
            "setup": s,
            "count": len(trades),
            "win_rate_pct": round(w / len(trades) * 100, 1) if trades else 0,
            "avg_pnl_pct": round(np.mean(p), 4) if p else 0,
            "total_pnl_pct": round(sum(p), 2),
        })

    # Profit factor
    gross_wins = sum(t["net_pnl_pct"] for t in all_trades if t["won"])
    gross_losses = abs(sum(t["net_pnl_pct"] for t in all_trades if not t["won"]))
    profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else float("inf")

    # Sharpe ratio (annualized, assuming daily returns)
    if len(pnls) > 1:
        sharpe = round(np.mean(pnls) / (np.std(pnls) + 1e-9) * np.sqrt(252), 2)
    else:
        sharpe = 0.0

    return {
        "summary": {
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(wins / total_trades * 100, 1) if total_trades > 0 else 0,
            "total_pnl_pct": round(sum(pnls), 2) if pnls else 0,
            "avg_pnl_pct": round(np.mean(pnls), 4) if pnls else 0,
            "profit_factor": profit_factor,
            "sharpe_ratio": sharpe,
            "max_drawdown_pct": round(max_dd, 2),
            "symbols_tested": len(symbols_to_run),
            "symbols_with_trades": len(symbol_summaries),
            "errors": len(errors),
            "timeframe": timeframe,
            "days": days,
        },
        "parameters": {
            "min_score": min_score,
            "conflict_margin": conflict_margin,
            "atr_mult": atr_mult,
            "fixed_sl_pct": fixed_sl_pct,
            "fixed_tp_pct": fixed_tp_pct,
            "force_fixed_sl": force_fixed_sl,
            "exit_at_t1": exit_at_t1,
            "slippage_pct": slippage_pct,
            "cost_pct": cost_pct,
        },
        "trades": all_trades[:500],  # Cap at 500 for payload size
        "equity_curve": equity_curve,
        "score_bands": score_bands,
        "direction_breakdown": {
            "long": _dir_stats(long_trades),
            "short": _dir_stats(short_trades),
        },
        "setup_breakdown": setup_breakdown,
        "symbol_summaries": symbol_summaries[:50],  # Top 50
        "errors": errors[:20],
    }
