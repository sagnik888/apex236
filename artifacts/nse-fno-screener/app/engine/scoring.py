"""Composite closed-bar trend/momentum scoring for the NSE F&O screener.

v3 audit changes:
- continuous multi-factor score instead of a handful of coarse binary votes;
- EMA stack and EMA slope are separate so trend *state* and trend *change* both matter;
- 3-bar + 10-bar momentum catches current impulse without ignoring the broader move;
- MACD level plus histogram acceleration;
- DMI supplies directional trend information while ADX remains confidence-only;
- breakout/breakdown is measured against the PRIOR range (no self-inclusion);
- RVOL compares the latest bar with PRIOR bars, so the spike does not dilute its own baseline;
- multi-factor disagreement dampens confidence to reduce mixed-regime false positives;
- optional higher-timeframe trend confirmation is a real signed input and is wired by pipeline.py.

No scoring formula can honestly claim a fixed "accuracy" percentage without a defined
forward-return target and out-of-sample validation.  This module therefore exposes every
component and ships with deterministic synthetic tests; see scoring_validation.py for
walk-forward evaluation hooks on real historical data.
"""
from dataclasses import dataclass
import numpy as np
import pandas as pd

from app import indicators as ind


@dataclass
class ScoreWeights:
    trend_alignment: float = 0.18
    trend_slope: float = 0.10
    momentum: float = 0.17
    macd: float = 0.12
    rsi_confirm: float = 0.08
    structure: float = 0.10
    dmi: float = 0.10
    breakout: float = 0.05
    htf_confirm: float = 0.10
    volume_sensitivity: float = 0.20
    adx_floor: float = 0.55
    adx_full_confidence_at: float = 30.0
    agreement_floor: float = 0.72
    bull_threshold: float = 22.0
    bear_threshold: float = -22.0
    strong_threshold: float = 62.0


WEIGHT_PROFILES = {
    "intraday": ScoreWeights(
        trend_alignment=0.15, trend_slope=0.10, momentum=0.20, macd=0.14,
        rsi_confirm=0.08, structure=0.08, dmi=0.10, breakout=0.07, htf_confirm=0.08,
    ),
    "swing": ScoreWeights(
        trend_alignment=0.20, trend_slope=0.11, momentum=0.14, macd=0.12,
        rsi_confirm=0.08, structure=0.10, dmi=0.10, breakout=0.05, htf_confirm=0.10,
    ),
    "positional": ScoreWeights(
        trend_alignment=0.22, trend_slope=0.13, momentum=0.10, macd=0.09,
        rsi_confirm=0.07, structure=0.12, dmi=0.10, breakout=0.05, htf_confirm=0.12,
    ),
}


def structure_score(high: pd.Series, low: pd.Series, close: pd.Series, lookback: int = 20) -> float:
    recent_high = high.iloc[-lookback:].max()
    recent_low = low.iloc[-lookback:].min()
    rng = recent_high - recent_low
    if not rng or np.isnan(rng) or rng == 0:
        return 0.0
    pos = (close.iloc[-1] - recent_low) / rng
    return float(np.clip((pos - 0.5) * 2, -1, 1))


def breakout_score(high: pd.Series, low: pd.Series, close: pd.Series, lookback: int = 20,
                   atr_value: float | None = None) -> float:
    """Signed breakout against the *previous* N bars, never the current bar."""
    if len(close) <= lookback:
        return 0.0
    prior_high = float(high.iloc[-lookback-1:-1].max())
    prior_low = float(low.iloc[-lookback-1:-1].min())
    c = float(close.iloc[-1])
    scale = float(atr_value or 0.0)
    if scale <= 0 or np.isnan(scale):
        scale = max((prior_high - prior_low) / 4.0, 1e-9)
    if c > prior_high:
        return float(np.clip((c - prior_high) / scale + 0.35, 0, 1))
    if c < prior_low:
        return -float(np.clip((prior_low - c) / scale + 0.35, 0, 1))
    return 0.0


def _tanh_norm(value: float, scale: float) -> float:
    if not scale or np.isnan(scale):
        return 0.0
    return float(np.tanh(value / scale))


def _weighted_agreement(terms: list[tuple[float, float]], direction: float) -> float:
    """0..1 share of meaningful weighted factors agreeing with composite direction."""
    sign = np.sign(direction)
    if sign == 0:
        return 0.5
    meaningful = [(v, w) for v, w in terms if abs(v) >= 0.08 and w > 0]
    if not meaningful:
        return 0.5
    total = sum(w for _, w in meaningful)
    agree = sum(w for v, w in meaningful if np.sign(v) == sign)
    return float(agree / total) if total else 0.5


def score_symbol_timeframe(
    bars: pd.DataFrame,
    weights: ScoreWeights = ScoreWeights(),
    momentum_lookback: int = 10,
    volume_lookback: int = 20,
    structure_lookback: int = 20,
    htf_trend_alignment: float | None = None,
) -> dict:
    min_bars_needed = max(65, momentum_lookback + 1, volume_lookback + 1, structure_lookback + 1)
    if len(bars) < min_bars_needed:
        return {"status": "insufficient_data", "bars_available": len(bars), "bars_needed": min_bars_needed}

    close, high, low, vol = bars["close"], bars["high"], bars["low"], bars["volume"]
    ema9, ema21, ema50 = ind.ema(close, 9), ind.ema(close, 21), ind.ema(close, 50)
    c, e9, e21, e50 = map(float, (close.iloc[-1], ema9.iloc[-1], ema21.iloc[-1], ema50.iloc[-1]))

    # Stack state is intentionally coarse; slope below adds continuous trend quality.
    trend_alignment = float((np.sign(c - e9) + np.sign(e9 - e21) + np.sign(e21 - e50)) / 3.0)

    atr14 = ind.atr(high, low, close, 14)
    atr_last = float(atr14.iloc[-1])
    if not atr_last or np.isnan(atr_last):
        return {"status": "zero_atr"}

    slope21 = _tanh_norm(float(ema21.iloc[-1] - ema21.iloc[-6]), atr_last * 1.25)
    slope50 = _tanh_norm(float(ema50.iloc[-1] - ema50.iloc[-11]), atr_last * 1.75)
    trend_slope = float(np.clip(0.65 * slope21 + 0.35 * slope50, -1, 1))

    short_mom = _tanh_norm(float(close.iloc[-1] - close.iloc[-4]), atr_last * 1.35)
    medium_mom = _tanh_norm(float(close.iloc[-1] - close.iloc[-1 - momentum_lookback]), atr_last * 2.8)
    momentum_norm = float(np.clip(0.40 * short_mom + 0.60 * medium_mom, -1, 1))

    _, _, hist = ind.macd(close)
    hist_last = float(hist.iloc[-1])
    hist_delta = float(hist.iloc[-1] - hist.iloc[-3])
    macd_level = _tanh_norm(hist_last, atr_last * 0.22)
    macd_accel = _tanh_norm(hist_delta, atr_last * 0.16)
    macd_score = float(np.clip(0.72 * macd_level + 0.28 * macd_accel, -1, 1))

    rsi14 = ind.rsi(close, 14)
    rsi_last = float(rsi14.iloc[-1])
    rsi_confirm = float(np.clip((rsi_last - 50.0) / 22.0, -1, 1))

    struct_score = structure_score(high, low, close, structure_lookback)
    breakout = breakout_score(high, low, close, structure_lookback, atr_last)

    adx14, plus_di, minus_di = ind.adx(high, low, close, 14)
    adx_last = float(adx14.iloc[-1])
    pdi, mdi = float(plus_di.iloc[-1]), float(minus_di.iloc[-1])
    dmi_denom = pdi + mdi
    dmi_score = float(np.clip((pdi - mdi) / dmi_denom, -1, 1)) if dmi_denom > 0 else 0.0

    # ADX is strength, never direction. Below ~10 the confidence is close to the floor;
    # at 30+ it reaches 1.0 without deleting otherwise-valid low-ADX reversals.
    adx_progress = float(np.clip((adx_last - 8.0) / max(1.0, weights.adx_full_confidence_at - 8.0), 0, 1))
    adx_confidence = weights.adx_floor + (1.0 - weights.adx_floor) * adx_progress

    # Baseline EXCLUDES the current bar. Including it muted the very spike being measured.
    baseline = vol.iloc[-volume_lookback-1:-1].replace(0, np.nan).dropna()
    avg_prior_vol = float(baseline.mean()) if len(baseline) else float("nan")
    current_vol = float(vol.iloc[-1])
    rvol = current_vol / avg_prior_vol if avg_prior_vol and not np.isnan(avg_prior_vol) else 1.0
    # Smooth, bounded confirmation: a huge print should strengthen the already-signed
    # move, not saturate every strong trend at +/-100 and destroy ranking resolution.
    volume_multiplier = 1.0 + weights.volume_sensitivity * float(np.tanh((rvol - 1.0) / 1.5))

    terms: list[tuple[float, float]] = [
        (trend_alignment, weights.trend_alignment),
        (trend_slope, weights.trend_slope),
        (momentum_norm, weights.momentum),
        (macd_score, weights.macd),
        (rsi_confirm, weights.rsi_confirm),
        (struct_score, weights.structure),
        (dmi_score, weights.dmi),
        (breakout, weights.breakout),
    ]
    if htf_trend_alignment is not None:
        terms.append((float(np.clip(htf_trend_alignment, -1, 1)), weights.htf_confirm))

    weight_sum = sum(w for _, w in terms)
    direction_raw = sum(v * w for v, w in terms) / weight_sum if weight_sum else 0.0
    agreement = _weighted_agreement(terms, direction_raw)
    agreement_multiplier = weights.agreement_floor + (1.0 - weights.agreement_floor) * agreement

    final = float(np.clip(direction_raw * adx_confidence * volume_multiplier * agreement_multiplier, -1, 1)) * 100.0

    if final >= weights.strong_threshold:
        label = "Strong Bull"
    elif final >= weights.bull_threshold:
        label = "Bull"
    elif final <= -weights.strong_threshold:
        label = "Strong Bear"
    elif final <= weights.bear_threshold:
        label = "Bear"
    else:
        label = "Neutral"

    return {
        "status": "ok",
        "score": round(final, 2),
        "label": label,
        "components": {
            "trend_alignment": round(trend_alignment, 3),
            "trend_slope": round(trend_slope, 3),
            "momentum_norm": round(momentum_norm, 3),
            "macd_score": round(macd_score, 3),
            "rsi_confirm": round(rsi_confirm, 3),
            "rsi_raw": round(rsi_last, 2),
            "structure_score": round(struct_score, 3),
            "dmi_score": round(dmi_score, 3),
            "plus_di": round(pdi, 2),
            "minus_di": round(mdi, 2),
            "breakout_score": round(breakout, 3),
            "htf_confirm": round(float(htf_trend_alignment), 3) if htf_trend_alignment is not None else None,
            "adx": round(adx_last, 2),
            "confidence": round(adx_confidence, 3),
            "agreement": round(agreement, 3),
            "agreement_multiplier": round(agreement_multiplier, 3),
            "rvol": round(float(rvol), 3),
            "volume_multiplier": round(volume_multiplier, 3),
        },
        "last_close": c,
        "last_bar_time": str(bars.index[-1]),
    }


def rank_universe(scores: dict, weights: ScoreWeights = ScoreWeights()) -> dict:
    bulls, bears, neutral = [], [], []
    for symbol, s in scores.items():
        if s.get("status") != "ok":
            continue
        row = {"symbol": symbol, **s}
        if s["score"] >= weights.bull_threshold:
            bulls.append(row)
        elif s["score"] <= weights.bear_threshold:
            bears.append(row)
        else:
            neutral.append(row)
    bulls.sort(key=lambda r: r["score"], reverse=True)
    bears.sort(key=lambda r: r["score"])
    neutral.sort(key=lambda r: abs(r["score"]))
    return {"bullish": bulls, "bearish": bears, "neutral": neutral}


def reason_bullets(components: dict, label: str) -> list[str]:
    out: list[str] = []
    if components.get("trend_alignment", 0) >= 0.66:
        out.append("EMA9/21/50 stack bullish")
    elif components.get("trend_alignment", 0) <= -0.66:
        out.append("EMA9/21/50 stack bearish")
    if components.get("trend_slope", 0) >= 0.35:
        out.append("EMA trend slope rising")
    elif components.get("trend_slope", 0) <= -0.35:
        out.append("EMA trend slope falling")
    if components.get("macd_score", 0) >= 0.35:
        out.append("MACD momentum bullish")
    elif components.get("macd_score", 0) <= -0.35:
        out.append("MACD momentum bearish")
    if components.get("dmi_score", 0) >= 0.20:
        out.append("+DI leads -DI")
    elif components.get("dmi_score", 0) <= -0.20:
        out.append("-DI leads +DI")
    if components.get("breakout_score", 0) > 0:
        out.append("Breakout above prior 20-bar range")
    elif components.get("breakout_score", 0) < 0:
        out.append("Breakdown below prior 20-bar range")
    if components.get("htf_confirm") is not None:
        if components["htf_confirm"] >= 0.33:
            out.append("Higher timeframe trend agrees bullish")
        elif components["htf_confirm"] <= -0.33:
            out.append("Higher timeframe trend agrees bearish")
        else:
            out.append("Higher timeframe trend mixed")
    if components.get("rvol", 1) >= 1.5:
        out.append(f"Volume confirms at {components['rvol']:.1f}x prior average")
    if components.get("adx", 0) < 15:
        out.append("Low ADX reduces confidence")
    if components.get("agreement", 1) < 0.60:
        out.append("Factors disagree — score dampened")
    if not out:
        out.append("Mixed signal with no dominant confirmation")
    return out
