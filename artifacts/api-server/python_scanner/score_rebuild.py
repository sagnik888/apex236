"""Train and validate the rebuilt score; compare it against the legacy score.

Protocol
--------
  TRAIN window (earlier 65% by time)  -> fit standardisation, ridge and
                                          logistic weights, percentile map
  TEST window  (later 35%, untouched) -> rank IC, AUC, decile lift, calibration

The legacy hand-weighted score is put through exactly the same out-of-sample
measurement so the comparison is like-for-like. A rebuilt score is only worth
shipping if it is measurably informative on the TEST window; otherwise the
correct conclusion is that these inputs do not carry rankable information.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from apex_python_scanner import ApexConfig, ApexScanner
from scoring_model import ApexScoreModel, auc, ic_significance, rank_ic

HERE = Path(__file__).resolve().parent
CACHE = HERE / "angel_cache"
MODEL_PATH = HERE / "apex_score_model.json"
COST = 0.182
TP, SL = 1.50, 0.75
TRAIN_FRAC = 0.65

FEATURES = [
    "legacy_score", "adx", "di_spread", "rsi_dir", "rvol", "atr_exp", "atr_pct",
    "body_pos_dir", "body_atr", "macd_hist_n", "dist_ema21", "dist_ema50",
    "dist_ema200", "dist_vwap", "hour", "is_long", "aligned_trend",
    "strong_pattern", "is_breakout", "is_breakdown", "is_trend_setup",
    "regime_1h_trend", "regime_1d_trend", "score_gap",
]


def load_cached(interval: str, limit: int):
    frames = {}
    for path in sorted(CACHE.glob(f"*__{interval}.pkl")):
        try:
            f = pd.read_pickle(path)
            if isinstance(f, pd.DataFrame) and len(f) > 400:
                frames[path.name.split("__")[0]] = f
        except Exception:
            continue
        if len(frames) >= limit:
            break
    return frames


def simulate(o, h, l, c, i, is_long, tp_pct, sl_pct, max_bars=200):
    if i + 1 >= len(o):
        return None
    entry = float(o[i + 1])
    if not np.isfinite(entry) or entry <= 0:
        return None
    sign = 1.0 if is_long else -1.0
    tp = entry * (1 + sign * tp_pct / 100.0)
    sl = entry * (1 - sign * sl_pct / 100.0)
    for k in range(i + 1, min(i + 1 + max_bars, len(o))):
        if is_long:
            if o[k] <= sl: return (o[k] - entry) / entry * 100.0
            if l[k] <= sl: return (sl - entry) / entry * 100.0
            if h[k] >= tp: return (tp - entry) / entry * 100.0
        else:
            if o[k] >= sl: return (entry - o[k]) / entry * 100.0
            if h[k] >= sl: return (entry - sl) / entry * 100.0
            if l[k] <= tp: return (entry - tp) / entry * 100.0
    last = float(c[min(i + max_bars, len(o) - 1)])
    return sign * (last - entry) / entry * 100.0


def build_dataset(limit: int) -> pd.DataFrame:
    universe = load_cached("15m", limit)
    cfg = ApexConfig(
        min_score=60.0, conflict_margin=12.0, use_htf=True, entry_delay_bars=1,
        realistic_fills=True, use_session=True, enforce_market_hours=True,
        keep_full_history=True, max_input_bars=0, strict_ohlcv=False,
        min_history_bars=210, fixed_sl_pct=0.5, force_fixed_sl=True,
        fixed_tp_pct=1.0, exit_at_t1=True, use_trail=False,
    )
    scanner = ApexScanner(cfg)
    rows = []
    for symbol, frame in universe.items():
        try:
            res = scanner.run_symbol(symbol, frame, asset_type="stock")
        except Exception:
            continue
        f = res.frame
        o, h, l, c = (f[x].to_numpy() for x in ("open", "high", "low", "close"))
        sig = f["signal"].to_numpy(); setup = f["setup"].to_numpy()
        get = lambda n: f[n].to_numpy() if n in f.columns else np.full(len(f), np.nan)
        score_c, bull, bear = get("signal_score"), get("bull_score"), get("bear_score")
        adx, dip, dim = get("adx"), get("di_plus"), get("di_minus")
        rsi, rvol, atr_exp = get("rsi"), get("relative_volume"), get("atr_expansion")
        body_pos, body_abs, atr = get("body_position"), get("body_abs"), get("atr")
        macd_hist = get("macd_hist")
        ema21, ema50, ema200, vwap = get("ema21"), get("ema50"), get("ema200"), get("vwap")
        reg1h, reg1d = get("regime_1h"), get("regime_1d")
        sb = f["pattern_strong_bull"].to_numpy() if "pattern_strong_bull" in f else np.zeros(len(f), bool)
        sr = f["pattern_strong_bear"].to_numpy() if "pattern_strong_bear" in f else np.zeros(len(f), bool)
        bull_tr, bear_tr = get("bull_trend"), get("bear_trend")
        hours = (f.index.hour + f.index.minute / 60.0).to_numpy()

        for i in np.flatnonzero(pd.Series(sig).isin(["BUY", "SELL"]).to_numpy()):
            i = int(i)
            out = simulate(o, h, l, c, i, sig[i] == "BUY", TP, SL)
            if out is None:
                continue
            is_long = sig[i] == "BUY"
            d = 1.0 if is_long else -1.0
            px = c[i]
            a = atr[i] if np.isfinite(atr[i]) and atr[i] > 0 else np.nan
            # Regime codes: base 1/3 = bullish trend, 2/4 = bearish. Convert to
            # a directional agreement term in [-1, 1].
            def reg_dir(code):
                if not np.isfinite(code):
                    return 0.0
                base = int(code) % 10
                if base in (1, 3): return 1.0 * d
                if base in (2, 4): return -1.0 * d
                return 0.0
            rows.append({
                "ts": f.index[i], "symbol": symbol, "net": out - COST,
                "legacy_score": score_c[i],
                "adx": adx[i],
                "di_spread": (dip[i] - dim[i]) * d,
                "rsi_dir": rsi[i] if is_long else 100 - rsi[i],
                "rvol": rvol[i],
                "atr_exp": atr_exp[i],
                "atr_pct": a / px * 100 if (np.isfinite(a) and px) else np.nan,
                "body_pos_dir": body_pos[i] if is_long else 1 - body_pos[i],
                "body_atr": body_abs[i] / a if np.isfinite(a) else np.nan,
                "macd_hist_n": macd_hist[i] / a * d if np.isfinite(a) else np.nan,
                "dist_ema21": (px - ema21[i]) / px * 100 * d,
                "dist_ema50": (px - ema50[i]) / px * 100 * d,
                "dist_ema200": (px - ema200[i]) / px * 100 * d,
                "dist_vwap": (px - vwap[i]) / px * 100 * d,
                "hour": hours[i],
                "is_long": 1.0 if is_long else 0.0,
                "aligned_trend": 1.0 if (bull_tr[i] if is_long else bear_tr[i]) else 0.0,
                "strong_pattern": 1.0 if (sb[i] if is_long else sr[i]) else 0.0,
                "is_breakout": 1.0 if setup[i] == "Breakout" else 0.0,
                "is_breakdown": 1.0 if setup[i] == "Breakdown" else 0.0,
                "is_trend_setup": 1.0 if setup[i] == "Trend" else 0.0,
                "regime_1h_trend": reg_dir(reg1h[i]),
                "regime_1d_trend": reg_dir(reg1d[i]),
                "score_gap": (bull[i] - bear[i]) * d,
            })
    return pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)


def decile_table(scores, net, title):
    print(f"\n  {title}")
    print(f"    {'decile':>7s} {'n':>6s} {'win%':>7s} {'expectancy':>12s}")
    for k in range(10):
        lo, hi = k * 10, (k + 1) * 10
        mask = (scores >= lo) & (scores <= hi if k == 9 else scores < hi)
        if mask.sum() < 10:
            continue
        sub = net[mask]
        print(f"    {k+1:7d} {int(mask.sum()):6d} {(sub>0).mean()*100:6.1f}% {sub.mean():+11.4f}%")


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    df = build_dataset(limit)
    df = df.replace([np.inf, -np.inf], np.nan)
    cut = df["ts"].quantile(TRAIN_FRAC)
    train, test = df[df["ts"] <= cut].copy(), df[df["ts"] > cut].copy()

    print(f"Dataset {len(df)} signals | geometry {TP}%/{SL}% | cost {COST}%")
    print(f"TRAIN {len(train)}  {train['ts'].min():%Y-%m-%d} to {train['ts'].max():%Y-%m-%d}")
    print(f"TEST  {len(test)}  {test['ts'].min():%Y-%m-%d} to {test['ts'].max():%Y-%m-%d}")

    Xtr = train[FEATURES].to_numpy(dtype=float)
    Xte = test[FEATURES].to_numpy(dtype=float)
    ytr = train["net"].to_numpy(dtype=float)
    yte = test["net"].to_numpy(dtype=float)

    model = ApexScoreModel().fit(Xtr, ytr, FEATURES, alpha=25.0)

    print(f"\n{'='*94}\nFITTED WEIGHTS (standardised units; ridge shrinks redundant inputs)\n{'='*94}")
    print(f"  {'feature':20s} {'ridge w':>10s} {'logit w':>10s}")
    for name, rw, lw in model.weight_table()[:14]:
        print(f"  {name:20s} {rw:+10.4f} {lw:+10.4f}")

    print(f"\n{'='*94}\nIN-SAMPLE (train) — expected to look good; not evidence of anything\n{'='*94}")
    rep_tr = model.evaluate(Xtr, ytr, "train")
    print(f"  rank IC={rep_tr['rank_ic']:+.4f} (t={rep_tr['ic_t_stat']:+.2f})  AUC={rep_tr['auc']:.4f}  "
          f"top-bottom={rep_tr['top_minus_bottom_expectancy']:+.4f}%")

    print(f"\n{'='*94}\nOUT-OF-SAMPLE (test) — the only result that counts\n{'='*94}")
    rep_te = model.evaluate(Xte, yte, "test")
    print(f"  REBUILT score: rank IC={rep_te['rank_ic']:+.4f} (t={rep_te['ic_t_stat']:+.2f})  "
          f"AUC={rep_te['auc']:.4f}  top-bottom={rep_te['top_minus_bottom_expectancy']:+.4f}%")

    legacy_te = test["legacy_score"].to_numpy(dtype=float)
    ok = np.isfinite(legacy_te)
    lic = rank_ic(legacy_te[ok], yte[ok])
    lauc = auc(legacy_te[ok], (yte[ok] > 0).astype(float))
    print(f"  LEGACY  score: rank IC={lic:+.4f} (t={ic_significance(lic, int(ok.sum())):+.2f})  "
          f"AUC={lauc:.4f}")

    decile_table(model.score(Xte), yte, "REBUILT score deciles (out-of-sample)")
    # Legacy score is saturated, so show its natural buckets rather than deciles.
    print(f"\n  LEGACY score buckets (out-of-sample)")
    print(f"    {'bucket':>10s} {'n':>6s} {'win%':>7s} {'expectancy':>12s}")
    for lo, hi in [(0, 70), (70, 80), (80, 90), (90, 95), (95, 101)]:
        mask = ok & (legacy_te >= lo) & (legacy_te < hi)
        if mask.sum() < 10:
            continue
        sub = yte[mask]
        print(f"    {f'{lo}-{hi}':>10s} {int(mask.sum()):6d} {(sub>0).mean()*100:6.1f}% {sub.mean():+11.4f}%")

    model.calibrate(Xte, yte, bins=5)
    print(f"\n  Calibration table (what a score band actually means, measured OOS):")
    for i, (lo, hi) in enumerate(model.calibration.bands):
        print(f"    score {lo:3.0f}-{hi:3.0f}: win {model.calibration.win_rate[i]:5.1f}%  "
              f"expectancy {model.calibration.expectancy[i]:+.4f}%  (n={model.calibration.count[i]})")

    model.save(MODEL_PATH)
    print(f"\n  Model saved to {MODEL_PATH.name}")

    print(f"\n{'='*94}\nVERDICT\n{'='*94}")
    informative = rep_te["informative"]
    print(f"  Rebuilt score informative out-of-sample: {'YES' if informative else 'NO'}")
    print(f"    (requires |IC t-stat| > 2.5 and AUC > 0.53; got t={rep_te['ic_t_stat']:+.2f}, AUC={rep_te['auc']:.4f})")
    if not informative:
        print("  The mathematics is now correct: standardised inputs, data-fitted weights,")
        print("  ridge-shrunk collinearity, percentile output that cannot saturate, and a")
        print("  calibration table. The inputs themselves carry no rankable information on")
        print("  this data, so the honest output is a flat score, not false confidence.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
