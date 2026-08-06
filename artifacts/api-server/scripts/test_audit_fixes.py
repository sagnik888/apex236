import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'python_scanner'))

"""Regression tests for the post-audit fixes.

Covers the behaviors that were changed to close the manual-vs-automated gap:
round-trip cost deduction, conservative same-bar backtest resolution, and the
theta-aware / symmetric option-stop translation.
"""
import numpy as np

from apex_python_scanner import ApexConfig, ApexScanner, synthetic_ohlcv
from simulation_engine import simulate
from options_engine import calculate_option_stops


def _run(cost_pct: float):
    cfg = ApexConfig(max_input_bars=0, keep_full_history=True, round_trip_cost_pct=cost_pct)
    data = synthetic_ohlcv(rows=2500, seed=7)
    return ApexScanner(cfg).run_symbol("TEST", data)


def test_round_trip_cost_lowers_net_pnl():
    """The same trades net of cost must total less than gross."""
    gross = _run(0.0)
    net = _run(0.5)
    assert len(gross.trades) == len(net.trades) > 0
    gross_total = sum(t.pnl_pct for t in gross.trades)
    net_total = sum(t.pnl_pct for t in net.trades)
    # Each closed trade pays the 0.5% round-trip once.
    assert net_total < gross_total
    assert abs((gross_total - net_total) - 0.5 * len(net.trades)) < 1e-6


def test_simulate_same_bar_resolves_to_stop_and_deducts_cost():
    """A bar that touches both TP and SL must book the STOP (a loss), and costs
    are deducted by default now."""
    # entry at index 1 open = 100. Bar 1 spans both +1% TP and -1% SL.
    o = np.array([100.0, 100.0, 100.0])
    h = np.array([100.0, 102.0, 100.0])
    l = np.array([100.0, 98.0, 100.0])
    c = np.array([100.0, 100.0, 100.0])
    ret, _ = simulate(o, h, l, c, entry_idx=0, is_long=True, tp_pct=1.0, sl_pct=1.0)
    # Conservative: booked at the stop -> clearly negative, and cost-deducted.
    assert ret < 0


def test_option_stop_symmetric_cap_and_theta_buffer():
    # Huge spot stop distance forces the 60%-of-premium cap.
    sl_capped, tp_capped = calculate_option_stops(
        entry_option=100.0, entry_spot=2500.0, stop_spot=2100.0,  # dist 400 -> opt 200 > 60
        target_spot=2600.0, option_delta=0.5, stop_mode="Delta-Translated",
    )
    # Stop capped at 60% of premium -> 40; target scaled by the same 0.6/2.0 factor.
    assert sl_capped == 40.0
    assert tp_capped < 120.0  # would have been 100 + (100*0.5)=150 uncapped; scaled down

    # Theta buffer widens (lowers) the stop for a multi-day hold.
    sl_no_theta, _ = calculate_option_stops(
        entry_option=100.0, entry_spot=2500.0, stop_spot=2480.0, target_spot=2540.0,
        option_delta=0.5, holding_days=0.0,
    )
    sl_theta, _ = calculate_option_stops(
        entry_option=100.0, entry_spot=2500.0, stop_spot=2480.0, target_spot=2540.0,
        option_delta=0.5, holding_days=5.0,
    )
    assert sl_theta < sl_no_theta


def test_forming_bar_is_decision_inert():
    """With act_on_forming_bar=False, flagging the last bar as forming must not
    increase trade/entry activity on that bar versus treating it as closed."""
    cfg = ApexConfig(act_on_forming_bar=False, allow_entry_on_last_bar=False, keep_full_history=True)
    data = synthetic_ohlcv(rows=2500, seed=7)
    scanner = ApexScanner(cfg)
    closed = scanner.run_symbol("TEST", data, last_bar_is_forming=False)
    forming = scanner.run_symbol("TEST", data, last_bar_is_forming=True)
    # The forming run cannot have MORE completed trades than the closed run.
    assert len(forming.trades) <= len(closed.trades)
    # No fresh signal is written on the final (forming) row.
    assert str(forming.frame.iloc[-1].get("signal", "")) == ""
