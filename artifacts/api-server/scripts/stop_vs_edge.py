import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'python_scanner'))

"""Is the stop destroying a real edge, or is there no edge at all?

The forward-return test found Breakout signals gained +0.28% over drift on an
8-bar horizon (t=7.4), yet every stop-and-target simulation lost money. Those
two facts are only compatible if the stop is being tagged by noise on paths
that ultimately resolve favourably.

This isolates the exit: same signals, but exit purely on TIME (hold N bars,
no stop, no target), net of costs, with the same train/test split. If
time-exits are profitable out-of-sample, the edge is real and the risk model
is the problem. If not, there is no edge to protect.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from apex_python_scanner import ApexConfig, ApexScanner

CACHE = Path(__file__).resolve().parent / "angel_cache"
COST = 0.182
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


def stats(x):
    x = np.asarray(x, dtype=float)
    if len(x) < 2:
        return 0.0, 0.0, len(x)
    m = float(x.mean()); se = float(x.std(ddof=1) / np.sqrt(len(x)))
    return m, (m / se if se > 0 else 0.0), len(x)


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    universe = load_cached("15m", limit)
    cfg = ApexConfig(
        min_score=60.0, conflict_margin=12.0, use_htf=True, entry_delay_bars=1,
        realistic_fills=True, use_session=True, enforce_market_hours=True,
        keep_full_history=True, max_input_bars=0, strict_ohlcv=False,
        min_history_bars=210, fixed_sl_pct=0.5, force_fixed_sl=True,
        fixed_tp_pct=1.0, exit_at_t1=True, use_trail=False,
    )
    scanner = ApexScanner(cfg)

    horizons = [4, 8, 16, 26, 52]      # 15m bars: 1h, 2h, 4h, ~1 day, ~2 days
    rows = []
    for symbol, frame in universe.items():
        try:
            res = scanner.run_symbol(symbol, frame, asset_type="stock")
        except Exception:
            continue
        f = res.frame
        o = f["open"].to_numpy(); c = f["close"].to_numpy()
        sig = f["signal"].to_numpy(); setup = f["setup"].to_numpy()
        idx = np.flatnonzero(pd.Series(sig).isin(["BUY", "SELL"]).to_numpy())
        for i in idx:
            i = int(i)
            if i + 1 >= len(o):
                continue
            entry = float(o[i + 1])
            if not np.isfinite(entry) or entry <= 0:
                continue
            is_long = sig[i] == "BUY"
            sign = 1.0 if is_long else -1.0
            rec = {"ts": f.index[i], "setup": str(setup[i]), "is_long": is_long}
            for h in horizons:
                j = min(i + 1 + h, len(c) - 1)
                rec[f"h{h}"] = sign * (float(c[j]) - entry) / entry * 100.0 - COST
            rows.append(rec)

    df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    cut = df["ts"].quantile(TRAIN_FRAC)
    train, test = df[df["ts"] <= cut], df[df["ts"] > cut]
    print(f"{len(df)} signals | TIME-EXIT ONLY (no stop, no target) | cost {COST}%")
    print(f"TRAIN {len(train)}  TEST {len(test)}\n")

    cohorts = {
        "ALL signals": lambda d: pd.Series(True, index=d.index),
        "LONG only": lambda d: d["is_long"],
        "BREAKOUT only": lambda d: d["setup"] == "Breakout",
        "BREAKDOWN only": lambda d: d["setup"] == "Breakdown",
        "TREND only": lambda d: d["setup"] == "Trend",
    }

    print(f"  {'cohort':18s} {'horizon':>8s} {'TRAIN exp':>11s} {'t':>6s} | {'TEST exp':>10s} {'t':>6s}  {'n_test':>6s}  verdict")
    for cname, pred in cohorts.items():
        tr, te = train[pred(train)], test[pred(test)]
        for h in horizons:
            mtr, ttr, ntr = stats(tr[f"h{h}"])
            mte, tte, nte = stats(te[f"h{h}"])
            if ntr < 100 or nte < 60:
                continue
            ok = mte > 0 and tte > 2.0
            bars_label = {4: "1h", 8: "2h", 16: "4h", 26: "~1d", 52: "~2d"}[h]
            print(f"  {cname:18s} {bars_label:>8s} {mtr:+10.4f}% {ttr:+6.2f} | {mte:+9.4f}% {tte:+6.2f}  {nte:6d}  "
                  f"{'SURVIVES' if ok else ''}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
