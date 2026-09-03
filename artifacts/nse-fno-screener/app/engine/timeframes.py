"""
engine/timeframes.py — Timeframe bucket definitions and bar-sealing resampling.

CRITICAL RULE: never hand a currently-forming bar to the scoring engine
as if it were closed. Every resample here drops the final bucket unless
its right edge has actually passed relative to the latest input bar —
this mirrors the "lookahead-sealed" MTF rule already used in this
project's Pine Script -> Python conversions (APEX Hybrid Pro). Skipping
it here means a "closed" 4H bar that's really still 40 minutes from
closing, which silently corrupts every score built on top of it.
"""
import pandas as pd

INTRADAY_TFS = ["5min", "15min", "30min", "1h"]
SWING_TFS = ["4h", "1d"]
POSITIONAL_TFS = ["2d", "4d", "1w"]

TF_GROUPS = {
    "intraday": INTRADAY_TFS,
    "swing": SWING_TFS,
    "positional": POSITIONAL_TFS,
}

# pandas resample rule strings for the timeframes that map directly onto
# a calendar-time resample of an intraday tick/bar series.
CALENDAR_RESAMPLE_RULES = {
    "5min": "5min",
    "15min": "15min",
    "30min": "30min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1D",
}

# "2-day" and "4-day" are NOT calendar days: NSE is closed weekends plus
# ~15 holidays/year, so a naive calendar '2D'/'4D' resample drifts
# against actual trading sessions (a "2-day" bar starting on a Thursday
# would span Thu+Fri+Sat+Sun under a calendar rule, silently becoming a
# 4-calendar-day/2-trading-day bar). Build these from a SESSION-indexed
# daily series instead (see build_session_indexed_daily below), so a
# '2D'/'4D' resample groups by trading sessions, not wall-clock days.
SESSION_BUCKET_SIZES = {"2d": 2, "4d": 4}


def resample_ohlcv(bars: pd.DataFrame, rule: str, seal: bool = True) -> pd.DataFrame:
    """
    bars: DataFrame indexed by bar-close timestamp (tz-aware, ideally
          Asia/Kolkata), columns [open, high, low, close, volume],
          ascending order.
    seal: if True (default), drop the last bucket unless its right edge
          is already <= the last input timestamp — i.e. never return a
          bucket that isn't actually closed yet.
    """
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = bars.resample(rule, label="right", closed="right").agg(agg).dropna(subset=["close"])

    if seal and len(out) > 0:
        last_bucket_end = out.index[-1]
        last_input_time = bars.index[-1]
        if last_input_time < last_bucket_end:
            out = out.iloc[:-1]  # final bucket hasn't actually closed yet
    return out


def build_session_indexed_daily(daily_bars: pd.DataFrame, valid_session_dates: set) -> pd.DataFrame:
    """
    daily_bars: a '1D' resample of intraday bars (already bar-sealed).
    valid_session_dates: set of actual NSE trading-session dates (from
        market_calendar), used to drop any stray non-session rows before
        session-counting — a defensive check, not the primary holiday
        filter (the data source shouldn't emit non-session bars in the
        first place, but don't trust that blindly).

    Returns the same data re-indexed 0..N-1 by trading-SESSION number
    instead of calendar date, so a downstream groupby(session_no // k)
    correctly buckets "2-day" / "4-day" windows by trading sessions.
    """
    d = daily_bars[daily_bars.index.normalize().isin(pd.to_datetime(sorted(valid_session_dates)))].copy()
    d = d.sort_index()
    d["session_no"] = range(len(d))
    return d


def session_bucket_resample(session_daily: pd.DataFrame, bucket_size: int, seal: bool = True) -> pd.DataFrame:
    """Group a session-indexed daily frame into fixed-size trading-session buckets (2d/4d)."""
    if session_daily.empty:
        return session_daily
    group_id = session_daily["session_no"] // bucket_size
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = session_daily.groupby(group_id).agg(agg)
    out.index = session_daily.groupby(group_id).apply(lambda g: g.index[-1])
    if seal and len(out) > 0:
        # last group is only "closed" if it actually has bucket_size sessions in it
        last_group_size = (group_id == group_id.iloc[-1]).sum()
        if last_group_size < bucket_size:
            out = out.iloc[:-1]
    return out
