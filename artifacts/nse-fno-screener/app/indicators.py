"""
indicators.py — Core technical indicator calculations.

All indicators operate on CLOSED bars only (see engine/timeframes.py for
bar-sealing rules). An in-progress bar must never be passed into these
functions as if it were a completed bar, or every multi-timeframe score
built on top of it inherits a silent lookahead bias.

Wilder-smoothed indicators (RSI, ATR, ADX/DI) use Wilder's original
recursive smoothing (alpha = 1/period), NOT a standard EMA (alpha =
2/(period+1)). This matches TradingView / Pine Script's built-in
ta.rsi(), ta.atr() and ta.dmi() — the reference implementation this
project's Pine Script indicators are already built against — so scores
computed here agree with what you already see on the chart, instead of
silently diverging by a few tenths of a point on every read.
"""
import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder's recursive smoothing: alpha = 1/period."""
    return series.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = wilder_smooth(gain, period)
    avg_loss = wilder_smooth(loss, period)

    # Explicitly handle the three zero-denominator regimes.  The previous
    # implementation filled every NaN with 100, which incorrectly classified a
    # perfectly flat market (zero gains AND zero losses) as maximally bullish.
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    both_zero = (avg_gain == 0) & (avg_loss == 0)
    gains_only = (avg_gain > 0) & (avg_loss == 0)
    losses_only = (avg_gain == 0) & (avg_loss > 0)
    out = out.mask(both_zero, 50.0)
    out = out.mask(gains_only, 100.0)
    out = out.mask(losses_only, 0.0)
    return out


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = true_range(high, low, close)
    return wilder_smooth(tr, period)


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14):
    """Returns (adx, plus_di, minus_di) — all Wilder-smoothed."""
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm = pd.Series(plus_dm, index=high.index)
    minus_dm = pd.Series(minus_dm, index=high.index)

    tr = true_range(high, low, close)
    atr_ = wilder_smooth(tr, period)
    plus_di = 100 * wilder_smooth(plus_dm, period) / atr_.replace(0, np.nan)
    minus_di = 100 * wilder_smooth(minus_dm, period) / atr_.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_ = wilder_smooth(dx.fillna(0), period)
    return adx_.fillna(0), plus_di.fillna(0), minus_di.fillna(0)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def roc(close: pd.Series, period: int) -> pd.Series:
    return (close / close.shift(period) - 1) * 100


def pct_change_n(close: pd.Series, n_bars: int) -> float:
    """% change from n_bars ago to the latest closed bar. NaN if not enough history."""
    if len(close) <= n_bars:
        return float("nan")
    return float((close.iloc[-1] / close.iloc[-1 - n_bars] - 1) * 100)


def intraday_pct_changes(bars_5min: pd.DataFrame, session_open_index: int = 0) -> dict:
    """
    Sub-day % change columns for the Intraday tab: 5m / 15m / 30m / 1h / 2h /
    since-open — deliberately NOT the 1D/3D/7D/.../60D calendar-window
    columns used on the Swing and Positional tabs (those stay exactly as
    they are; this is a separate column set for a separate audience
    question: "what has this done in the last few minutes/hours", not
    "what has this done in the last few weeks").

    Always computed from the 5-MINUTE bar series specifically, regardless
    of which intraday sub-tab (5min/15min/1h) is currently selected for
    scoring — these are properties of the symbol's price action right
    now, not of which chart granularity you happen to be looking at, so
    a given symbol shows the same 5m/15m/.../since-open numbers on all
    three intraday sub-tabs. Using the 5-min series (rather than
    resampling from whichever timeframe is active) gives exact clock-time
    alignment: a "30 minute" change is exactly 6 five-minute bars back on
    every sub-tab, instead of being approximated differently depending on
    which timeframe happens to be selected.

    bars_5min: closed, bar-sealed 5-minute OHLCV bars for the CURRENT
        trading session only (see market_calendar.py for session bounds).
    session_open_index: row index of the first bar of today's session —
        0 if bars_5min has already been sliced to just today.
    """
    close = bars_5min["close"]
    n = len(close)
    windows_in_bars = {"chg_5min": 1, "chg_15min": 3, "chg_30min": 6, "chg_1h": 12, "chg_2h": 24}
    out = {}
    for key, n_bars in windows_in_bars.items():
        out[key] = pct_change_n(close, n_bars) if n > n_bars else float("nan")
    if n > session_open_index:
        session_open_price = close.iloc[session_open_index]
        out["chg_since_open"] = float((close.iloc[-1] / session_open_price - 1) * 100)
    else:
        out["chg_since_open"] = float("nan")
    return out
