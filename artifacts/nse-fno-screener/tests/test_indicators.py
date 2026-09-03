"""
Synthetic-data tests. No network access is required or used — these
validate the math itself (Wilder smoothing bounds, RSI/ADX ranges, no
lookahead in resampling), not live connectivity to any data source.
"""
import numpy as np
import pandas as pd
import pytest

from app import indicators as ind
from app.engine import timeframes as tf


def make_trending_bars(n=200, start=100.0, drift=0.15, noise=0.6, seed=42):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01 09:15", periods=n, freq="15min", tz="Asia/Kolkata")
    steps = drift + rng.normal(0, noise, n)
    close = start + np.cumsum(steps)
    high = close + rng.uniform(0.1, 0.8, n)
    low = close - rng.uniform(0.1, 0.8, n)
    open_ = close - steps + rng.normal(0, 0.2, n)
    volume = rng.integers(50_000, 150_000, n).astype(float)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


def test_rsi_bounds():
    bars = make_trending_bars()
    r = ind.rsi(bars["close"], 14)
    valid = r.dropna()
    assert (valid >= 0).all() and (valid <= 100).all()


def test_rsi_uptrend_above_50():
    bars = make_trending_bars(drift=0.3, noise=0.2)  # strong clean uptrend
    r = ind.rsi(bars["close"], 14)
    assert r.iloc[-1] > 50, "a clean, low-noise uptrend should show RSI above 50"


def test_adx_range():
    bars = make_trending_bars()
    adx14, plus_di, minus_di = ind.adx(bars["high"], bars["low"], bars["close"], 14)
    valid = adx14.dropna()
    assert (valid >= 0).all() and (valid <= 100).all()


def test_atr_positive():
    bars = make_trending_bars()
    a = ind.atr(bars["high"], bars["low"], bars["close"], 14)
    assert (a.dropna() > 0).all()


def test_macd_shapes():
    bars = make_trending_bars()
    macd_line, signal_line, hist = ind.macd(bars["close"])
    assert len(macd_line) == len(bars)
    assert np.allclose((macd_line - signal_line).dropna(), hist.dropna())


def test_intraday_pct_changes_uses_correct_bar_offsets():
    # construct 5-min bars with a KNOWN, exact close at each offset so the
    # windows can be checked precisely, not just "some plausible number"
    n = 30
    idx = pd.date_range("2026-01-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    closes = pd.Series([100 + i for i in range(n)], index=idx, dtype=float)  # close[i] = 100+i
    bars = pd.DataFrame({
        "open": closes, "high": closes + 0.1, "low": closes - 0.1,
        "close": closes, "volume": [1000.0] * n,
    }, index=idx)

    result = ind.intraday_pct_changes(bars, session_open_index=0)
    last = closes.iloc[-1]  # 100 + 29 = 129

    assert result["chg_5min"] == pytest.approx((last / closes.iloc[-2] - 1) * 100)     # 1 bar back
    assert result["chg_15min"] == pytest.approx((last / closes.iloc[-4] - 1) * 100)    # 3 bars back
    assert result["chg_30min"] == pytest.approx((last / closes.iloc[-7] - 1) * 100)    # 6 bars back
    assert result["chg_1h"] == pytest.approx((last / closes.iloc[-13] - 1) * 100)      # 12 bars back
    assert result["chg_2h"] == pytest.approx((last / closes.iloc[-25] - 1) * 100)      # 24 bars back
    assert result["chg_since_open"] == pytest.approx((last / closes.iloc[0] - 1) * 100)


def test_intraday_pct_changes_same_across_subtabs_by_construction():
    """
    The whole point of computing these from the 5-min series only: calling
    this function doesn't take a "which sub-tab" argument at all, so it's
    structurally impossible for 5m/15m/1h sub-tabs to disagree on a
    symbol's chg_since_open the way the original per-timeframe random
    sample data accidentally could have.
    """
    import inspect
    params = inspect.signature(ind.intraday_pct_changes).parameters
    assert "timeframe" not in params and "tf" not in params


def test_resample_seals_incomplete_final_bar():
    """
    The critical no-lookahead test: if the input series stops midway
    through what would be the final 1h bucket, that bucket must NOT
    appear in the resampled output.
    """
    idx = pd.date_range("2026-01-01 09:15", periods=5, freq="15min", tz="Asia/Kolkata")
    # 09:15, 09:30, 09:45, 10:00, 10:15 -> the 09:15-10:15 hourly bucket
    # (closes at 10:15 under label='right') is NOT complete because we
    # don't have data up to 10:15 fully closing into the next bucket edge
    bars = pd.DataFrame({
        "open": [1, 2, 3, 4, 5], "high": [1, 2, 3, 4, 5],
        "low": [1, 2, 3, 4, 5], "close": [1, 2, 3, 4, 5],
        "volume": [10, 10, 10, 10, 10],
    }, index=idx)
    sealed = tf.resample_ohlcv(bars, "1h", seal=True)
    unsealed = tf.resample_ohlcv(bars, "1h", seal=False)
    assert len(sealed) < len(unsealed), (
        "sealing must drop the still-forming final bucket that the "
        "unsealed resample incorrectly includes"
    )


def test_rsi_flat_market_is_neutral_50_not_100():
    idx = pd.date_range("2026-01-01 09:15", periods=80, freq="15min", tz="Asia/Kolkata")
    close = pd.Series([100.0] * len(idx), index=idx)
    r = ind.rsi(close, 14)
    assert r.iloc[-1] == pytest.approx(50.0)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
