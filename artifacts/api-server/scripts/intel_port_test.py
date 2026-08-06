"""Does the index system's market-intel actually add edge to the STOCK signals?

Ports the two self-contained modules from "ut index 2" (price_action.py and
session_context.py — both numpy/pandas only, no coupling to that repo) and
measures, strictly out-of-sample, whether their features add information the
current stock features lack.

Rationale: the stock scanner's existing inputs are all standard lagging
transforms of one price series and were shown to carry no rankable edge.
price_action supplies a genuinely different class — swing structure, horizontal
S/R, premium/discount position, liquidity pools — so it is worth measuring
rather than assuming, in either direction.

Comparison (identical train/test split, identical model):
    BASELINE  = existing stock features
    +INTEL    = existing features + price-action / session-context features
Reported: per-day rank IC (Fama-MacBeth), AUC, and top-N selection P&L.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

INDEX_REPO = Path(r"C:\Users\sagnik\Desktop\ut index 2")
if str(INDEX_REPO) not in sys.path:
    sys.path.insert(0, str(INDEX_REPO))

from apex_python_scanner import ApexConfig, ApexScanner
from score_rebuild import FEATURES as BASE_FEATURES, TRAIN_FRAC, COST, TP, SL, load_cached, simulate
from scoring_model import ApexScoreModel, auc, rank_ic

try:
    from intelligence.price_action import PriceActionAnalyzer
    from intelligence.session_context import SessionContextAnalyzer
    INTEL_OK = True
except Exception as exc:  # pragma: no cover
    print(f"Could not import index intelligence modules: {exc!r}")
    INTEL_OK = False

INTEL_FEATURES = [
    "pa_score", "pa_dist_res", "pa_dist_sup", "pa_zone_pos", "pa_struct_up",
    "pa_struct_dn", "pa_eq_liquidity", "pa_rr_to_level",
    "pa_dist_poc", "pa_in_value_area", "pa_aroon_osc", "pa_aroon_late",
    "pa_wt_bias", "pa_wt_exhaustion", "pa_structure_event",
    "sc_gap_pct", "sc_or_break", "sc_vwap_side",
]


def pa_features(analyzer, window: pd.DataFrame, is_long: bool) -> dict:
    """Direction-aware price-action features for one signal bar.

    Key names follow price_action.analyze()'s actual return contract
    (nearest_resistance / nearest_support / zone / equal_highs / ...).
    """
    blank = {k: 0.0 for k in INTEL_FEATURES}
    try:
        out = analyzer.analyze(window, "BUY" if is_long else "SELL",
                               use_premium_discount=True, use_liquidity=True,
                               use_bos_choch=True)
    except Exception:
        out = None
    if not out:
        return blank
    close = float(window["close"].iloc[-1])
    d = 1.0 if is_long else -1.0
    nr, ns = out.get("nearest_resistance"), out.get("nearest_support")
    struct = str(out.get("structure", "")).lower()

    dist_res = (float(nr) - close) / close * 100 if nr else np.nan
    dist_sup = (close - float(ns)) / close * 100 if ns else np.nan
    # Room to the objective vs distance back to protection — the structural
    # reward:risk the stock system has no concept of.
    target_dist = dist_res if is_long else dist_sup
    stop_dist = dist_sup if is_long else dist_res

    zone = out.get("zone")
    zone_pos = 0.5
    if isinstance(zone, (list, tuple)) and len(zone) == 2:
        zone_pos = float(zone[1])
    elif isinstance(zone, dict):
        zone_pos = float(zone.get("position", 0.5))
    elif isinstance(zone, (int, float)):
        zone_pos = float(zone)

    poc = out.get("poc"); vah = out.get("vah"); val = out.get("val")
    dist_poc = (close - float(poc)) / close * 100 * d if poc else 0.0
    in_va = 1.0 if (vah and val and float(val) <= close <= float(vah)) else 0.0

    aroon = out.get("aroon") or {}
    wt = out.get("wavetrend") or {}
    wt_bias = str(wt.get("bias", "")).lower()
    ev_dir = str(out.get("structure_event_dir", "")).lower()

    eqh, eql = out.get("equal_highs"), out.get("equal_lows")
    # A pool of equal highs above a long (or equal lows below a short) is a
    # stop-hunt magnet sitting between entry and target.
    eq_risk = 1.0 if ((is_long and eqh) or ((not is_long) and eql)) else 0.0

    blank.update({
        "pa_score": float(out.get("score", 0.0) or 0.0),
        "pa_dist_res": dist_res if np.isfinite(dist_res) else 0.0,
        "pa_dist_sup": dist_sup if np.isfinite(dist_sup) else 0.0,
        # Direction-aware: high zone position is chasing for a long, ideal for a short.
        "pa_zone_pos": (zone_pos - 0.5) * 2.0 * d,
        "pa_struct_up": 1.0 if "up" in struct else 0.0,
        "pa_struct_dn": 1.0 if "down" in struct else 0.0,
        "pa_eq_liquidity": eq_risk,
        "pa_rr_to_level": (target_dist / stop_dist) if (np.isfinite(target_dist) and np.isfinite(stop_dist) and stop_dist > 0.01) else 0.0,
        "pa_dist_poc": dist_poc,
        "pa_in_value_area": in_va,
        "pa_aroon_osc": float(aroon.get("oscillator", 0.0) or 0.0) / 100.0 * d if aroon.get("available") else 0.0,
        "pa_aroon_late": 1.0 if aroon.get("late_trend_caution") else 0.0,
        "pa_wt_bias": (1.0 if "bull" in wt_bias else -1.0 if "bear" in wt_bias else 0.0) * d,
        "pa_wt_exhaustion": 1.0 if wt.get("exhaustion") else 0.0,
        "pa_structure_event": (1.0 if "up" in ev_dir or "bull" in ev_dir else
                               -1.0 if "down" in ev_dir or "bear" in ev_dir else 0.0) * d,
    })
    return blank


def sc_features(analyzer, day_df: pd.DataFrame, is_long: bool, prev_close: float) -> dict:
    out = {}
    try:
        res = analyzer.analyze(day_df, spot_price=float(day_df["close"].iloc[-1]),
                               previous_close=float(prev_close))
    except Exception:
        res = None
    d = 1.0 if is_long else -1.0
    if not res:
        return {"sc_gap_pct": 0.0, "sc_or_break": 0.0, "sc_vwap_side": 0.0}
    gap = float(res.get("gap_pct", 0.0) or 0.0)
    orb = str(res.get("opening_range_state", res.get("or_state", "")) or "")
    vwap_side = str(res.get("vwap_state", res.get("vwap_side", "")) or "")
    out["sc_gap_pct"] = gap * d
    out["sc_or_break"] = (1.0 if "break" in orb.lower() and "up" in orb.lower() else
                          -1.0 if "break" in orb.lower() and "down" in orb.lower() else 0.0) * d
    out["sc_vwap_side"] = (1.0 if "above" in vwap_side.lower() else
                           -1.0 if "below" in vwap_side.lower() else 0.0) * d
    return out


def build(limit: int) -> pd.DataFrame:
    universe = load_cached("15m", limit)
    cfg = ApexConfig(
        min_score=60.0, conflict_margin=12.0, use_htf=True, entry_delay_bars=1,
        realistic_fills=True, use_session=True, enforce_market_hours=True,
        keep_full_history=True, max_input_bars=0, strict_ohlcv=False,
        min_history_bars=210, fixed_sl_pct=0.5, force_fixed_sl=True,
        fixed_tp_pct=1.0, exit_at_t1=True, use_trail=False,
    )
    scanner = ApexScanner(cfg)
    pa = PriceActionAnalyzer() if INTEL_OK else None
    sc = SessionContextAnalyzer() if INTEL_OK else None

    from score_rebuild import build_dataset  # reuse the identical base builder
    base = build_dataset(limit)
    if not INTEL_OK:
        return base

    # Recompute intel features on the same (symbol, ts) keys.
    index = {(r.symbol, r.ts): i for i, r in enumerate(base.itertuples())}
    extra = {k: np.zeros(len(base)) for k in INTEL_FEATURES}
    done = 0
    for symbol, frame in universe.items():
        try:
            res = scanner.run_symbol(symbol, frame, asset_type="stock")
        except Exception:
            continue
        f = res.frame
        sig = f["signal"].to_numpy()
        for i in np.flatnonzero(pd.Series(sig).isin(["BUY", "SELL"]).to_numpy()):
            i = int(i)
            key = (symbol, f.index[i])
            pos = index.get(key)
            if pos is None:
                continue
            is_long = sig[i] == "BUY"
            window = f.iloc[max(0, i - 120):i + 1][["open", "high", "low", "close", "volume"]]
            feats = pa_features(pa, window, is_long)
            day_mask = f.index.normalize() == f.index[i].normalize()
            day_df = f.loc[day_mask & (f.index <= f.index[i])][["open", "high", "low", "close", "volume"]]
            prev_days = f.loc[f.index.normalize() < f.index[i].normalize(), "close"]
            prev_close = float(prev_days.iloc[-1]) if len(prev_days) else float(window["close"].iloc[0])
            feats.update(sc_features(sc, day_df, is_long, prev_close))
            for k, v in feats.items():
                extra[k][pos] = v if np.isfinite(v) else 0.0
            done += 1
    print(f"  intel features computed for {done} signals")
    # Coverage guard: an all-zero column means extraction silently failed and
    # the feature contributes nothing (this hid several broken keys once).
    print("  feature coverage (non-zero share):")
    for k, v in extra.items():
        nz = float(np.mean(np.asarray(v) != 0.0)) * 100
        flag = "  <-- DEAD" if nz < 1.0 else ""
        print(f"    {k:22s} {nz:5.1f}%{flag}")
        base[k] = v
    return base


def evaluate(df: pd.DataFrame, features: list[str], label: str) -> dict:
    cut = df["ts"].quantile(TRAIN_FRAC)
    train, test = df[df["ts"] <= cut].copy(), df[df["ts"] > cut].copy()
    model = ApexScoreModel().fit(
        train[features].to_numpy(float), train["net"].to_numpy(float), features, alpha=25.0
    )
    pred = model.expected_return(test[features].to_numpy(float))
    net = test["net"].to_numpy(float)
    test = test.assign(pred=pred, day=test["ts"].dt.date)

    daily = []
    for _, g in test.groupby("day"):
        g = g[np.isfinite(g["pred"])]
        if len(g) >= 10:
            daily.append(rank_ic(g["pred"].to_numpy(float), g["net"].to_numpy(float)))
    daily = np.array(daily)
    ic_m = float(daily.mean()) if len(daily) else 0.0
    ic_t = float(ic_m / (daily.std(ddof=1) / np.sqrt(len(daily)))) if len(daily) > 2 else 0.0

    tops = []
    for _, g in test.groupby("day"):
        g = g[np.isfinite(g["pred"])]
        if len(g) >= 8:
            tops.append(g.nlargest(8, "pred")["net"].to_numpy(float))
    top = np.concatenate(tops) if tops else np.array([0.0])
    top_m = float(top.mean())
    top_t = float(top_m / (top.std(ddof=1) / np.sqrt(len(top)))) if len(top) > 2 else 0.0

    return {
        "label": label, "n_test": len(test),
        "per_day_ic": ic_m, "ic_t": ic_t,
        "auc": auc(model.win_probability(test[features].to_numpy(float)), (net > 0).astype(float)),
        "base_exp": float(net.mean()),
        "top8_exp": top_m, "top8_t": top_t,
        "model": model, "features": features,
    }


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    print("Building dataset with ported index-intelligence features...")
    df = build(limit).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    print(f"Dataset: {len(df)} signals\n")

    runs = [evaluate(df, BASE_FEATURES, "BASELINE (stock features)")]
    if INTEL_OK:
        runs.append(evaluate(df, BASE_FEATURES + INTEL_FEATURES, "+ INDEX INTEL (price action + session)"))
        runs.append(evaluate(df, INTEL_FEATURES, "INTEL ONLY"))

    print(f"{'configuration':42s} {'per-day IC':>11s} {'t':>6s} {'AUC':>7s} {'top8/day exp':>13s} {'t':>6s}")
    print("-" * 96)
    for r in runs:
        print(f"{r['label']:42s} {r['per_day_ic']:+10.4f} {r['ic_t']:+6.2f} {r['auc']:6.4f} "
              f"{r['top8_exp']:+12.4f}% {r['top8_t']:+6.2f}")
    print(f"\n  out-of-sample base rate (trade everything): {runs[0]['base_exp']:+.4f}%")

    if len(runs) > 1:
        d_ic = runs[1]["per_day_ic"] - runs[0]["per_day_ic"]
        d_top = runs[1]["top8_exp"] - runs[0]["top8_exp"]
        print(f"\n  INTEL contribution: per-day IC {d_ic:+.4f}, top-8 expectancy {d_top:+.4f}%")
        improved = d_ic > 0.02 and runs[1]["ic_t"] > 2.0
        print(f"  Verdict: {'INTEL ADDS MEASURABLE INFORMATION' if improved else 'no material improvement from porting'}")
        print(f"\n  Top intel feature weights (fitted):")
        weights = dict((n, w) for n, w, _ in runs[1]["model"].weight_table())
        for name in sorted(INTEL_FEATURES, key=lambda n: -abs(weights.get(n, 0.0)))[:8]:
            print(f"    {name:22s} {weights.get(name, 0.0):+.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
