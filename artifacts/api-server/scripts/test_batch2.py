import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'python_scanner'))

"""Regression tests for batch-2: trade-style toggle, top-N selection + portfolio
circuit breaker, OCO reconciliation, and the option-leg P&L estimate."""
from unittest.mock import patch, MagicMock

import pytest

from apex_python_scanner import ApexConfig, ApexScanner, synthetic_ohlcv, estimate_option_pnl_pct
import scanner_engine as se
import options_engine as oe
from settings_store import validate


# ── Trade-style toggle ──────────────────────────────────────────────────────

def test_timeframes_for_style_alignment():
    assert se.timeframes_for_style("intraday", ["15m", "1h", "4h", "1d"]) == ["15m", "1h"]
    assert se.timeframes_for_style("swing", ["15m", "1h", "4h", "1d"]) == ["1h", "4h", "1d"]
    # Mismatched selection never yields an empty scan.
    assert se.timeframes_for_style("swing", ["15m"]) == ["15m"]


def test_trade_style_validation():
    assert validate({"trade_style": "btst"})["trade_style"] == "btst"
    with pytest.raises(ValueError):
        validate({"trade_style": "scalp"})


def test_intraday_style_has_no_overnight_holds():
    data = synthetic_ohlcv(rows=2500, seed=7)
    cfg = ApexConfig(trade_style="intraday", timeframe="15m", keep_full_history=True)
    res = ApexScanner(cfg).run_symbol("T", data)
    for t in res.trades:
        assert str(t.entry_time)[:10] == str(t.exit_time)[:10], "intraday trade held overnight"


def test_config_rejects_bad_trade_style():
    with pytest.raises(ValueError):
        ApexConfig(trade_style="nonsense").validate()


# ── Top-N selection + circuit breaker ───────────────────────────────────────

def test_apply_selection_respects_caps():
    eng = se.ScannerEngine()  # empty _results -> circuit not paused, 0 active
    sigs = [
        {"symbol": "A", "sector": "IT", "score": 90, "state": "PENDING"},
        {"symbol": "B", "sector": "IT", "score": 85, "state": "PENDING"},
        {"symbol": "C", "sector": "IT", "score": 80, "state": "PENDING"},
        {"symbol": "D", "sector": "BANK", "score": 88, "state": "PENDING"},
        {"symbol": "E", "sector": "BANK", "score": 70, "state": "PENDING"},
        {"symbol": "F", "sector": "BANK", "score": 60, "state": "PENDING"},
    ]
    summary = eng._apply_selection(sigs)
    selected = {s["symbol"] for s in sigs if s["selected"]}
    # max_per_sector=2 -> only top-2 per sector selected (A,B from IT; D,E from BANK)
    assert selected == {"A", "B", "D", "E"}
    assert summary["selected_count"] == 4
    assert not summary["circuit_breaker"]["paused"]


def test_circuit_state_empty_is_not_paused():
    eng = se.ScannerEngine()
    state = eng._portfolio_circuit_state()
    assert state["paused"] is False
    assert state["active_count"] == 0


# ── OCO reconciliation ──────────────────────────────────────────────────────

def test_oco_reconcile_cancels_sibling_of_filled_leg():
    oe._OPEN_OCO_PAIRS.clear()
    oe.register_oco_pair("STOP1", "TGT1", broker="upstox", symbol="X")
    disp = MagicMock()
    disp.get_order_status.side_effect = lambda oid, broker=None: (
        {"data": {"status": "complete"}} if oid == "TGT1" else {"data": {"status": "open"}}
    )
    with patch("options_engine.get_dispatcher", return_value=disp):
        actions = oe.reconcile_open_ocos()
    disp.cancel_order.assert_called_once_with("STOP1", broker="upstox")
    assert actions and actions[0]["filled"] == "target"
    assert oe._OPEN_OCO_PAIRS == []  # resolved pair removed from the watch list


def test_oco_keeps_unfilled_pair():
    oe._OPEN_OCO_PAIRS.clear()
    oe.register_oco_pair("STOP2", "TGT2", broker="upstox", symbol="Y")
    disp = MagicMock()
    disp.get_order_status.return_value = {"data": {"status": "open"}}
    with patch("options_engine.get_dispatcher", return_value=disp):
        actions = oe.reconcile_open_ocos()
    assert actions == []
    disp.cancel_order.assert_not_called()
    assert len(oe._OPEN_OCO_PAIRS) == 1  # still watching


# ── Option-leg P&L ──────────────────────────────────────────────────────────

def test_option_pnl_haircut_vs_cash():
    # A marginal cash win is a net option LOSS after theta + spread.
    assert estimate_option_pnl_pct(100.0, 0.1, 6, "15m") < 0
    # A strong cash win is a leveraged option gain.
    assert estimate_option_pnl_pct(100.0, 2.0, 6, "15m") > 20
    # Longer holds bleed more theta.
    short = estimate_option_pnl_pct(100.0, 1.0, 2, "1d")
    long = estimate_option_pnl_pct(100.0, 1.0, 20, "1d")
    assert long < short
