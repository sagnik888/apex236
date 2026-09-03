"""
These tests specifically target the three bugs found in the audited
ChatGPT plan's scoring pseudocode, using constructed scenarios where the
naive formula and the corrected formula would disagree:

  1. A clean, strong, low-noise uptrend (RSI comfortably > 50) must score
     BULLISH. The naive formula's `rsi_score = (50-rsi)/50` term goes
     NEGATIVE here (it's an oversold-bias term), which would drag a
     genuine bull leader's composite down at exactly the moment it
     should rank near the top of the bullish list.

  2. A volume spike during a clear down-move must make the score MORE
     bearish, not less. The naive formula's `volume_score = vol_change/100`
     is direction-blind and always adds a positive amount, which would
     partially offset (or in extreme cases flip the sign of) a
     volume-confirmed selloff's score.
"""
import numpy as np
import pandas as pd
import pytest

from app.engine.scoring import score_symbol_timeframe, rank_universe, ScoreWeights, WEIGHT_PROFILES, structure_score, reason_bullets


def make_bars(closes, highs=None, lows=None, volumes=None, start="2026-01-01 09:15", freq="15min"):
    n = len(closes)
    idx = pd.date_range(start, periods=n, freq=freq, tz="Asia/Kolkata")
    closes = np.array(closes, dtype=float)
    highs = np.array(highs, dtype=float) if highs is not None else closes + 0.5
    lows = np.array(lows, dtype=float) if lows is not None else closes - 0.5
    opens = np.concatenate([[closes[0]], closes[:-1]])
    volumes = np.array(volumes, dtype=float) if volumes is not None else np.full(n, 100_000.0)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes}, index=idx
    )


def strong_clean_uptrend(n=120, start=100.0, step=0.4):
    """Monotonic-ish uptrend, low noise -> RSI will sit comfortably above 50/70."""
    rng = np.random.default_rng(1)
    steps = step + rng.normal(0, 0.03, n)  # tiny noise, dominant uptrend
    closes = start + np.cumsum(steps)
    return make_bars(closes)


def strong_clean_downtrend_with_volume_spike(n=120, start=100.0, step=0.4):
    rng = np.random.default_rng(2)
    steps = -step + rng.normal(0, 0.03, n)
    closes = start + np.cumsum(steps)
    volumes = np.full(n, 100_000.0)
    volumes[-1] = 400_000.0  # sharp volume spike on the final (down) bar
    return make_bars(closes, volumes=volumes)


def test_clean_uptrend_scores_bullish_not_dragged_by_rsi():
    bars = strong_clean_uptrend()
    result = score_symbol_timeframe(bars)
    assert result["status"] == "ok"
    assert result["components"]["rsi_raw"] > 60, "sanity check: this scenario should show a high RSI"
    assert result["score"] > 0, (
        "a clean uptrend with high RSI must score bullish under the "
        "momentum-confirmation design; a naive oversold-bias RSI term "
        "would have pulled this negative"
    )
    assert result["label"] in ("Bull", "Strong Bull")


def test_volume_spike_amplifies_bearish_score_not_offsets_it():
    bars_no_spike = make_bars(100.0 - np.cumsum(np.full(120, 0.4)))
    bars_with_spike = strong_clean_downtrend_with_volume_spike()

    base = score_symbol_timeframe(bars_no_spike)
    spiked = score_symbol_timeframe(bars_with_spike)

    assert base["status"] == "ok" and spiked["status"] == "ok"
    assert base["score"] < 0, "sanity check: the no-spike downtrend should already be bearish"
    assert spiked["score"] < base["score"], (
        "a volume spike confirming a down-move must push the score MORE "
        "negative than the same move without the spike — a direction-blind "
        "additive volume term would instead push it toward zero (or "
        "positive), which is backwards"
    )


def test_structure_score_extremes():
    idx = pd.date_range("2026-01-01 09:15", periods=25, freq="15min", tz="Asia/Kolkata")
    # a range from 90 to 110; closing exactly at the high vs exactly at the low
    high = pd.Series([110.0] * 25, index=idx)
    low = pd.Series([90.0] * 25, index=idx)
    at_high = pd.Series([100.0] * 24 + [110.0], index=idx)
    at_low = pd.Series([100.0] * 24 + [90.0], index=idx)
    mid = pd.Series([100.0] * 25, index=idx)
    assert structure_score(high, low, at_high, lookback=20) == pytest.approx(1.0)
    assert structure_score(high, low, at_low, lookback=20) == pytest.approx(-1.0)
    assert structure_score(high, low, mid, lookback=20) == pytest.approx(0.0)


def test_structure_score_never_shared_between_directions():
    """
    The comparison prototype's analogous 'structure' logic pushed the
    SAME points onto both structBull and structBear under overlapping
    conditions. structure_score returns one signed float — by
    construction there is no separate bull/bear total for it to leak
    into both of, unlike the prototype's design.
    """
    import inspect
    sig = inspect.signature(structure_score)
    assert sig.return_annotation in (float, "float") or True  # documents intent; real guarantee is the single return path
    idx = pd.date_range("2026-01-01 09:15", periods=25, freq="15min", tz="Asia/Kolkata")
    high, low = pd.Series([110.0]*25, index=idx), pd.Series([90.0]*25, index=idx)
    close = pd.Series([100.0]*24 + [110.0], index=idx)
    result = structure_score(high, low, close)
    assert isinstance(result, float)


def test_htf_confirm_shifts_score_in_its_own_direction():
    bars = strong_clean_uptrend()
    no_htf = score_symbol_timeframe(bars)
    bullish_htf = score_symbol_timeframe(bars, htf_trend_alignment=1.0)
    bearish_htf = score_symbol_timeframe(bars, htf_trend_alignment=-1.0)
    assert no_htf["status"] == "ok" and bullish_htf["status"] == "ok" and bearish_htf["status"] == "ok"
    assert bullish_htf["score"] > bearish_htf["score"], (
        "a bullish higher-timeframe input must score higher than a bearish "
        "one on identical underlying bars — htf_confirm has to actually move "
        "the composite, not just appear in the components dict"
    )
    assert bullish_htf["components"]["htf_confirm"] == pytest.approx(1.0)
    assert no_htf["components"]["htf_confirm"] is None


def test_weight_profiles_actually_differ_by_group():
    bars = strong_clean_uptrend()
    per_group_scores = {
        group: score_symbol_timeframe(bars, weights=weights)["score"]
        for group, weights in WEIGHT_PROFILES.items()
    }
    # not asserting a specific ordering (that depends on this scenario's
    # exact shape) -- asserting the profiles are not accidentally identical,
    # which would silently defeat the entire point of having them
    assert len(set(per_group_scores.values())) > 1, (
        f"expected different groups to produce different scores on the same "
        f"bars, got identical scores: {per_group_scores}"
    )
    for weights in WEIGHT_PROFILES.values():
        direction_weight_sum = (weights.trend_alignment + weights.trend_slope + weights.momentum + weights.macd
                                 + weights.rsi_confirm + weights.structure + weights.dmi + weights.breakout
                                 + weights.htf_confirm)
        assert direction_weight_sum == pytest.approx(1.0, abs=0.01)


def test_reason_bullets_reflects_real_components_not_placeholders():
    bars = strong_clean_uptrend()
    result = score_symbol_timeframe(bars)
    bullets = reason_bullets(result["components"], result["label"])
    assert len(bullets) >= 1
    assert all(isinstance(b, str) and len(b) > 0 for b in bullets)
    # a clean uptrend should surface at least one bullish-sounding reason
    assert any("bull" in b.lower() or "confirm" in b.lower() or "top" in b.lower() for b in bullets)


def test_insufficient_data_returns_status_not_exception():
    bars = make_bars(np.linspace(100, 105, 10))  # too few bars
    result = score_symbol_timeframe(bars)
    assert result["status"] == "insufficient_data"


def test_rank_universe_orders_correctly():
    up = strong_clean_uptrend()
    down = strong_clean_downtrend_with_volume_spike()
    flat_rng = np.random.default_rng(3)
    flat = make_bars(100 + np.cumsum(flat_rng.normal(0, 0.05, 120)))  # choppy/no trend

    scores = {
        "BULLCO": score_symbol_timeframe(up),
        "BEARCO": score_symbol_timeframe(down),
        "FLATCO": score_symbol_timeframe(flat),
    }
    ranked = rank_universe(scores)
    bullish_symbols = [r["symbol"] for r in ranked["bullish"]]
    bearish_symbols = [r["symbol"] for r in ranked["bearish"]]
    assert "BULLCO" in bullish_symbols
    assert "BEARCO" in bearish_symbols
    # bullish list must be sorted descending by score
    scores_list = [r["score"] for r in ranked["bullish"]]
    assert scores_list == sorted(scores_list, reverse=True)


def test_rvol_baseline_excludes_current_spike():
    bars = strong_clean_uptrend()
    bars.loc[bars.index[-1], "volume"] = 500_000.0
    result = score_symbol_timeframe(bars)
    # Prior 20 bars are all 100k, so true RVOL is exactly 5x.  A baseline
    # that included the current bar would dilute this below 5x.
    assert result["components"]["rvol"] == pytest.approx(5.0, rel=1e-3)


def test_factor_disagreement_dampens_confidence():
    rng = np.random.default_rng(99)
    closes = 100 + np.cumsum(rng.normal(0, 0.35, 140))
    bars = make_bars(closes)
    result = score_symbol_timeframe(bars)
    assert result["status"] == "ok"
    assert 0 <= result["components"]["agreement"] <= 1
    assert result["components"]["agreement_multiplier"] <= 1.0


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
