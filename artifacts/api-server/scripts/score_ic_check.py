import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'python_scanner'))

"""Is the rebuilt score's IC real, or an artifact of cross-sectional correlation?

A pooled t-stat assumes independent observations. These are not: on any given
bar dozens of correlated NSE names signal together, so one market-wide move
contributes many "independent-looking" rows. The standard remedy (Fama-MacBeth)
is to compute the information coefficient WITHIN each period, then test the
time series of per-period ICs. That reduces the effective sample to the number
of periods, which is the honest unit of independence here.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from score_rebuild import FEATURES, TRAIN_FRAC, build_dataset
from scoring_model import ApexScoreModel, rank_ic

HERE = Path(__file__).resolve().parent


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    df = build_dataset(limit).replace([np.inf, -np.inf], np.nan)
    cut = df["ts"].quantile(TRAIN_FRAC)
    train, test = df[df["ts"] <= cut].copy(), df[df["ts"] > cut].copy()

    model = ApexScoreModel().fit(
        train[FEATURES].to_numpy(float), train["net"].to_numpy(float), FEATURES, alpha=25.0
    )
    test["pred"] = model.expected_return(test[FEATURES].to_numpy(float))
    test["legacy"] = test["legacy_score"]

    n_sig = len(test)
    n_days = test["ts"].dt.date.nunique()
    n_bars = test["ts"].nunique()
    print(f"TEST window: {n_sig} signals across {n_bars} distinct bars / {n_days} trading days")
    print(f"  -> mean {n_sig/max(1,n_bars):.1f} signals per bar; the pooled t-stat treats these as independent.\n")

    for label, col in (("REBUILT", "pred"), ("LEGACY", "legacy")):
        pooled = rank_ic(test[col].to_numpy(float), test["net"].to_numpy(float))
        pooled_t = pooled * np.sqrt((n_sig - 2) / max(1e-12, 1 - pooled**2))

        # Fama-MacBeth: IC per day, then t-test the series of daily ICs.
        daily = []
        for _, g in test.groupby(test["ts"].dt.date):
            g = g[np.isfinite(g[col]) & np.isfinite(g["net"])]
            if len(g) >= 10:
                daily.append(rank_ic(g[col].to_numpy(float), g["net"].to_numpy(float)))
        daily = np.array(daily)
        if len(daily) > 2:
            m = daily.mean()
            t = m / (daily.std(ddof=1) / np.sqrt(len(daily)))
        else:
            m, t = 0.0, 0.0
        verdict = "REAL" if abs(t) > 2.0 and m > 0 else "NOT SIGNIFICANT"
        print(f"{label} score")
        print(f"  pooled IC      = {pooled:+.4f}  t={pooled_t:+.2f}   <- assumes independence (overstated)")
        print(f"  per-day IC     = {m:+.4f}  t={t:+.2f}  over {len(daily)} days  -> {verdict}")
        print(f"  days IC > 0    = {int((daily > 0).sum())}/{len(daily)} ({(daily > 0).mean()*100:.0f}%)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
