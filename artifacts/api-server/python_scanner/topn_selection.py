"""Does within-day ranking convert into money?

The rebuilt score has a real per-day cross-sectional IC (+0.111, t=3.80).
That is RELATIVE skill: it orders today's candidates. It does not imply any
bucket is profitable, because the base rate is negative for every bucket.

This measures the practical version of the question: each day, take only the
top-N signals by score and trade those. Reported out-of-sample, net of costs,
against the all-signals base rate and against a random-N control.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from score_rebuild import FEATURES, TRAIN_FRAC, build_dataset
from scoring_model import ApexScoreModel
from sectors import get_sector

RNG = np.random.default_rng(7)


def summarise(vals: np.ndarray) -> tuple[float, float, int]:
    if len(vals) < 2:
        return 0.0, 0.0, len(vals)
    m = float(vals.mean())
    se = float(vals.std(ddof=1) / np.sqrt(len(vals)))
    se = float(vals.std(ddof=1) / np.sqrt(len(vals)))
    return m, (m / se if se > 0 else 0.0), len(vals)


def select_top_n_with_sector_cap(g: pd.DataFrame, n_top: int, max_per_sector: int = 2) -> list[float]:
    """Select top N trades for a day, enforcing a max_per_sector cap (bypassing 'NSE' fallback)."""
    g_sorted = g.sort_values("pred", ascending=False)
    picked_rows = []
    sector_counts: dict[str, int] = {}
    for _, row in g_sorted.iterrows():
        sec = get_sector(str(row["symbol"]))
        if sec == "NSE" or sector_counts.get(sec, 0) < max_per_sector:
            picked_rows.append(row["net"])
            if sec != "NSE":
                sector_counts[sec] = sector_counts.get(sec, 0) + 1
        if len(picked_rows) == n_top:
            break
    return picked_rows


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    df = build_dataset(limit).replace([np.inf, -np.inf], np.nan)
    cut = df["ts"].quantile(TRAIN_FRAC)
    train, test = df[df["ts"] <= cut].copy(), df[df["ts"] > cut].copy()

    model = ApexScoreModel().fit(
        train[FEATURES].to_numpy(float), train["net"].to_numpy(float), FEATURES, alpha=25.0
    )
    for frame in (train, test):
        frame["pred"] = model.expected_return(frame[FEATURES].to_numpy(float))
        frame["day"] = frame["ts"].dt.date

    base_m, base_t, base_n = summarise(test["net"].to_numpy(float))
    print(f"OUT-OF-SAMPLE base rate (trade everything): exp={base_m:+.4f}%  t={base_t:+.2f}  n={base_n}")
    print(f"Test window: {test['day'].nunique()} trading days, "
          f"{len(test)/test['day'].nunique():.0f} signals/day average\n")

    print(f"{'selection':28s} {'n':>6s} {'exp/trade':>11s} {'t':>7s} {'win%':>7s} {'total':>10s}  vs base")
    print("-" * 92)

    for n_top in (1, 2, 3, 5, 8, 12, 20):
        picked, rand = [], []
        for _, g in test.groupby("ts"):
            g = g[np.isfinite(g["pred"])]
            if len(g) < n_top:
                continue
            # Enforce max_per_sector=2 limit when selecting candidates (HIGH-14)
            picked_rows = select_top_n_with_sector_cap(g, n_top)
            if len(picked_rows) < n_top:
                continue
            picked.append(np.array(picked_rows, dtype=float))
            rand.append(g.sample(n=n_top, random_state=int(RNG.integers(1e6)))["net"].to_numpy(float))
        if not picked:
            continue
        p = np.concatenate(picked); r = np.concatenate(rand)
        pm, pt, pn = summarise(p)
        rm, _, _ = summarise(r)
        flag = "  <-- PROFITABLE" if pm > 0 and pt > 2.0 else ("  (positive)" if pm > 0 else "")
        print(f"top {n_top:2d}/day{'':16s} {pn:6d} {pm:+10.4f}% {pt:+7.2f} {(p>0).mean()*100:6.1f}% "
              f"{p.sum():+9.1f}% {pm-base_m:+7.4f}{flag}")
        print(f"  random {n_top}/day control{'':6s} {len(r):6d} {rm:+10.4f}%")

    # Bottom-N: if the ranking is real, the worst-ranked should be clearly worse.
    print()
    for n_bot in (5, 12):
        worst = []
        for _, g in test.groupby("ts"):
            g = g[np.isfinite(g["pred"])]
            if len(g) >= n_bot:
                worst.append(g.nsmallest(n_bot, "pred")["net"].to_numpy(float))
        if worst:
            w = np.concatenate(worst)
            wm, wt, wn = summarise(w)
            print(f"bottom {n_bot}/day (reject these) {wn:6d} {wm:+10.4f}% {wt:+7.2f} {(w>0).mean()*100:6.1f}%")

    # Long-only variant, since shorts were reliably negative.
    print()
    tl = test[test["is_long"] == 1.0]
    for n_top in (3, 5, 8):
        picked = []
        for _, g in tl.groupby("ts"):
            g = g[np.isfinite(g["pred"])]
            picked_rows = select_top_n_with_sector_cap(g, n_top)
            if len(picked_rows) == n_top:
                picked.append(np.array(picked_rows, dtype=float))
        if picked:
            p = np.concatenate(picked)
            pm, pt, pn = summarise(p)
            flag = "  <-- PROFITABLE" if pm > 0 and pt > 2.0 else ("  (positive)" if pm > 0 else "")
            print(f"top {n_top}/day LONG only{'':10s} {pn:6d} {pm:+10.4f}% {pt:+7.2f} {(p>0).mean()*100:6.1f}% "
                  f"{p.sum():+9.1f}%{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
