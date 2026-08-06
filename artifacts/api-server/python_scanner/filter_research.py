"""Can the system rank signals and trade only the good ones?

Method (designed to resist self-deception):
  1. Build a feature table for every signal, using ONLY information available
     at the signal bar, paired with the realised net-of-cost trade outcome.
  2. Split by TIME: the earlier 65% is the training window, the later 35% is
     held out and never consulted while choosing a filter.
  3. Univariate quintile scan on TRAIN only — which features rank outcomes?
  4. Take the best candidate rules from TRAIN and evaluate them ONCE on TEST.
  5. Report how many rules were searched, so the reader can judge how much
     multiple-comparison luck is baked into the best training result.

A filter is only credible if it survives step 4 with a positive expectancy
and a t-stat that is not explainable by the number of rules searched.
"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from apex_python_scanner import ApexConfig, ApexScanner
from simulation_engine import simulate

CACHE = Path(__file__).resolve().parent / "angel_cache"
COST = 0.182
TP, SL = 1.50, 0.75          # least-bad geometry from the previous sweep
TRAIN_FRAC = 0.65


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
        sig = f["signal"].to_numpy()
        idx = np.flatnonzero(pd.Series(sig).isin(["BUY", "SELL"]).to_numpy())
        if len(idx) == 0:
            continue
        col = {name: f[name].to_numpy() for name in (
            "signal_score", "bull_score", "bear_score", "adx", "di_plus", "di_minus",
            "rsi", "relative_volume", "atr_expansion", "body_position", "body_abs",
            "atr", "macd_hist", "ema21", "ema50", "ema200", "vwap", "close",
            "regime_15m", "regime_1h", "regime_4h", "regime_1d",
        ) if name in f.columns}
        setup = f["setup"].to_numpy()
        volreg = f["volatility_regime"].to_numpy()
        strong_bull = f["pattern_strong_bull"].to_numpy() if "pattern_strong_bull" in f else np.zeros(len(f), bool)
        strong_bear = f["pattern_strong_bear"].to_numpy() if "pattern_strong_bear" in f else np.zeros(len(f), bool)
        bull_trend = f["bull_trend"].to_numpy(); bear_trend = f["bear_trend"].to_numpy()
        hours = (f.index.hour + f.index.minute / 60.0).to_numpy()
        for i in idx:
            i = int(i)
            is_long = sig[i] == "BUY"
            # Intraday trades must square off by EOD (15:30). Each 15m bar = 0.25h.
            bars_to_eod = 200
            h_val = hours[i]
            if h_val < 15.5:
                bars_to_eod = int(max(1, (15.5 - h_val) * 4)) - 1
                if bars_to_eod <= 0:
                    continue
            sim_res = simulate(o, h, l, c, i, is_long, TP, SL, max_bars=bars_to_eod, deduct_costs=False)
            if sim_res is None:
                continue
            out, _ = sim_res
            px = col["close"][i]
            rows.append({
                "ts": f.index[i], "symbol": symbol, "is_long": is_long,
                "net": out - COST,
                "score": col["signal_score"][i],
                "adx": col["adx"][i],
                "di_spread": (col["di_plus"][i] - col["di_minus"][i]) * (1 if is_long else -1),
                "rsi": col["rsi"][i] if is_long else 100 - col["rsi"][i],
                "rvol": col["relative_volume"][i],
                "atr_exp": col["atr_expansion"][i],
                "atr_pct": col["atr"][i] / px * 100 if px else np.nan,
                "body_pos": col["body_position"][i] if is_long else 1 - col["body_position"][i],
                "body_atr": col["body_abs"][i] / col["atr"][i] if col["atr"][i] else np.nan,
                "macd_hist_n": col["macd_hist"][i] / col["atr"][i] * (1 if is_long else -1) if col["atr"][i] else np.nan,
                "dist_ema21": (px - col["ema21"][i]) / px * 100 * (1 if is_long else -1),
                "dist_ema50": (px - col["ema50"][i]) / px * 100 * (1 if is_long else -1),
                "dist_ema200": (px - col["ema200"][i]) / px * 100 * (1 if is_long else -1),
                "dist_vwap": (px - col["vwap"][i]) / px * 100 * (1 if is_long else -1),
                "regime_1h": col.get("regime_1h", np.full(len(f), np.nan))[i],
                "regime_1d": col.get("regime_1d", np.full(len(f), np.nan))[i],
                "setup": str(setup[i]),
                "volreg": str(volreg[i]),
                "strong_pattern": bool(strong_bull[i] if is_long else strong_bear[i]),
                "aligned_trend": bool(bull_trend[i] if is_long else bear_trend[i]),
                "hour": hours[i],
            })
    return pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)


def stats(x: np.ndarray) -> tuple[float, float, int]:
    if len(x) < 2:
        return 0.0, 0.0, len(x)
    m = float(x.mean())
    se = float(x.std(ddof=1) / np.sqrt(len(x)))
    return m, (m / se if se > 0 else 0.0), len(x)


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    df = build_dataset(limit)
    cut = df["ts"].quantile(TRAIN_FRAC)
    train, test = df[df["ts"] <= cut], df[df["ts"] > cut]
    print(f"Dataset: {len(df)} signals | geometry {TP}%/{SL}% | cost {COST}%")
    print(f"TRAIN {len(train)} signals  {train['ts'].min():%Y-%m-%d} to {train['ts'].max():%Y-%m-%d}")
    print(f"TEST  {len(test)} signals  {test['ts'].min():%Y-%m-%d} to {test['ts'].max():%Y-%m-%d}")
    m, t, n = stats(train["net"].to_numpy()); print(f"\nBaseline TRAIN: exp={m:+.4f}% t={t:+.2f}")
    m, t, n = stats(test["net"].to_numpy());  print(f"Baseline TEST : exp={m:+.4f}% t={t:+.2f}")

    numeric = ["score", "adx", "di_spread", "rsi", "rvol", "atr_exp", "atr_pct",
               "body_pos", "body_atr", "macd_hist_n", "dist_ema21", "dist_ema50",
               "dist_ema200", "dist_vwap", "hour"]

    print(f"\n{'='*104}\nUNIVARIATE QUINTILE SCAN — TRAIN ONLY (exploratory)\n{'='*104}")
    print(f"  {'feature':14s} " + "".join(f"{'Q'+str(i+1):>16s}" for i in range(5)) + "   spread")
    ranked = []
    for feat in numeric:
        sub = train[np.isfinite(train[feat])]
        if len(sub) < 250:
            continue
        try:
            q = pd.qcut(sub[feat], 5, labels=False, duplicates="drop")
        except Exception:
            continue
        cells = []
        for k in range(5):
            vals = sub.loc[q == k, "net"].to_numpy()
            cells.append(stats(vals)[0] if len(vals) > 20 else np.nan)
        if all(np.isfinite(cells)):
            spread = cells[-1] - cells[0]
            ranked.append((abs(spread), feat, cells, spread))
            print(f"  {feat:14s} " + "".join(f"{c:+15.3f}%" for c in cells) + f"  {spread:+7.3f}%")
    ranked.sort(reverse=True)

    # Categorical
    print(f"\n  {'setup':22s} {'n':>6s} {'exp':>10s} {'t':>7s}")
    for name, g in train.groupby("setup"):
        if len(g) >= 60:
            m, t, n = stats(g["net"].to_numpy())
            print(f"  {name:22s} {n:6d} {m:+9.3f}% {t:+7.2f}")

    # ---- Candidate rule search on TRAIN -------------------------------
    print(f"\n{'='*104}\nRULE SEARCH ON TRAIN (then a single evaluation on TEST)\n{'='*104}")
    top_feats = [f for _, f, _, _ in ranked[:6]]
    candidates = []
    for feat in top_feats:
        vals = train[feat].replace([np.inf, -np.inf], np.nan).dropna()
        for pct in (50, 60, 70, 80, 90):
            thr = float(np.percentile(vals, pct))
            candidates.append((f"{feat} >= p{pct} ({thr:.2f})", lambda d, f=feat, t=thr: d[f] >= t))
            thr2 = float(np.percentile(vals, 100 - pct))
            candidates.append((f"{feat} <= p{100-pct} ({thr2:.2f})", lambda d, f=feat, t=thr2: d[f] <= t))
    for s in train["setup"].unique():
        candidates.append((f"setup == {s}", lambda d, s=s: d["setup"] == s))
    candidates.append(("long only", lambda d: d["is_long"]))
    candidates.append(("aligned_trend", lambda d: d["aligned_trend"]))
    candidates.append(("strong_pattern", lambda d: d["strong_pattern"]))
    # pairwise combinations of the strongest singles
    singles = []
    for label, fn in candidates:
        sel = train[fn(train)]
        if len(sel) >= 150:
            m, t, n = stats(sel["net"].to_numpy())
            singles.append((m, t, n, label, fn))
    singles.sort(reverse=True)
    pairs = []
    for (m1, t1, n1, l1, f1), (m2, t2, n2, l2, f2) in itertools.combinations(singles[:8], 2):
        fn = lambda d, a=f1, b=f2: a(d) & b(d)
        sel = train[fn(train)]
        if len(sel) >= 120:
            m, t, n = stats(sel["net"].to_numpy())
            pairs.append((m, t, n, f"{l1} AND {l2}", fn))
    pairs.sort(reverse=True)
    searched = len(singles) + len(pairs)

    print(f"  Rules searched: {searched} ({len(singles)} single, {len(pairs)} pairs)")
    print(f"\n  Best on TRAIN:")
    print(f"  {'rule':58s} {'n':>5s} {'train exp':>11s} {'t':>6s}")
    best = (singles + pairs)
    best.sort(reverse=True)
    for m, t, n, label, fn in best[:8]:
        print(f"  {label:58s} {n:5d} {m:+10.4f}% {t:+6.2f}")

    print(f"\n  >>> OUT-OF-SAMPLE EVALUATION (test window never used above) <<<")
    print(f"  {'rule':58s} {'n':>5s} {'TEST exp':>11s} {'t':>6s}  verdict")
    survivors = 0
    for m, t, n, label, fn in best[:8]:
        sel = test[fn(test)]
        if len(sel) < 40:
            print(f"  {label:58s} {len(sel):5d}      too few")
            continue
        mt, tt, nt = stats(sel["net"].to_numpy())
        ok = mt > 0 and tt > 2.0
        survivors += ok
        print(f"  {label:58s} {nt:5d} {mt:+10.4f}% {tt:+6.2f}  {'SURVIVES' if ok else 'fails'}")

    print(f"\n  Rules surviving out-of-sample: {survivors}/8")
    print(f"  With {searched} rules searched, expect ~{searched*0.05:.0f} to look good on TRAIN by chance alone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
