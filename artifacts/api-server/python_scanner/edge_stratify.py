"""Where does the 15m edge live? Stratify signal forward-returns.

The pooled 15m signal beats random by only ~0.04%/trade while a round trip
costs ~0.18%. This asks whether the edge concentrates in a tradeable subset:
by score, setup type, direction, volatility regime, or time of day.

Reports edge relative to the same-period market drift (the sample window is
a rising market, so raw returns flatter every long).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from apex_python_scanner import ApexConfig, ApexScanner

CACHE = Path(__file__).resolve().parent / "angel_cache"
COST = 0.182  # round-trip charges + slippage, %


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


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 80
    interval = "15m"
    horizon = 8
    universe = load_cached(interval, limit)
    cfg = ApexConfig(
        min_score=60.0, conflict_margin=12.0, use_htf=True, entry_delay_bars=1,
        realistic_fills=True, use_session=True, enforce_market_hours=True,
        keep_full_history=True, max_input_bars=0, strict_ohlcv=False,
        min_history_bars=210, fixed_sl_pct=0.5, force_fixed_sl=True,
        fixed_tp_pct=1.0, exit_at_t1=True, use_trail=False,
    )
    scanner = ApexScanner(cfg)

    rows = []
    drift_all = []
    for symbol, frame in universe.items():
        try:
            res = scanner.run_symbol(symbol, frame, asset_type="stock")
        except Exception:
            continue
        f = res.frame
        o = f["open"].to_numpy()
        h = f["high"].to_numpy()
        l = f["low"].to_numpy()
        c = f["close"].to_numpy()
        fwd = np.full(len(c), np.nan)
        fwd[:-horizon] = (c[horizon:] - c[:-horizon]) / c[:-horizon] * 100.0
        valid = fwd[210:-horizon]
        drift_all.append(valid[np.isfinite(valid)])
        sig = f["signal"].to_numpy()
        score = f["signal_score"].to_numpy()
        setup = f["setup"].to_numpy()
        vol = f["volatility_regime"].to_numpy()
        adx = f["adx"].to_numpy()
        rvol = f["relative_volume"].to_numpy()
        hours = f.index.hour + f.index.minute / 60.0
        from simulation_engine import simulate
        for i in np.flatnonzero(pd.Series(sig).isin(["BUY", "SELL"]).to_numpy()):
            is_long = sig[i] == "BUY"
            sim_res = simulate(o, h, l, c, i, is_long, 100.0, 100.0, max_bars=horizon, deduct_costs=False)
            if sim_res is None:
                continue
            out, _ = sim_res
            rows.append({
                "dir": "LONG" if is_long else "SHORT",
                "ret": out,
                "score": float(score[i]) if np.isfinite(score[i]) else np.nan,
                "setup": str(setup[i]),
                "vol": str(vol[i]),
                "adx": float(adx[i]) if np.isfinite(adx[i]) else np.nan,
                "rvol": float(rvol[i]) if np.isfinite(rvol[i]) else np.nan,
                "hour": float(hours[i]),
            })
    df = pd.DataFrame(rows)
    drift = float(np.concatenate(drift_all).mean())
    print(f"{interval} — {len(universe)} symbols, {len(df)} signals, market drift over {horizon} bars = {drift:+.4f}%")
    print(f"Edge must exceed the {COST}% round-trip cost to be tradeable.\n")

    def block(title, groups):
        print(f"--- {title} " + "-" * (98 - len(title)))
        print(f"  {'bucket':22s} {'n':>6s} {'mean ret':>10s} {'vs drift':>10s} {'t':>7s} {'net of cost':>12s}")
        min_samples = 25
        merged_groups = []
        curr_name, curr_sub = None, None
        for name, sub in groups:
            if curr_sub is None:
                curr_name, curr_sub = str(name), sub
            elif len(curr_sub) < min_samples:
                curr_name = f"{curr_name}+{name}"
                curr_sub = pd.concat([curr_sub, sub], ignore_index=True)
            else:
                merged_groups.append((curr_name, curr_sub))
                curr_name, curr_sub = str(name), sub
        if curr_sub is not None:
            if len(curr_sub) < min_samples and merged_groups:
                prev_name, prev_sub = merged_groups.pop()
                merged_groups.append((f"{prev_name}+{curr_name}", pd.concat([prev_sub, curr_sub], ignore_index=True)))
            else:
                merged_groups.append((curr_name, curr_sub))

        for name, sub in merged_groups:
            if len(sub) < min_samples:
                continue
            m = sub["ret"].mean()
            edge = m - drift
            t = m / (sub["ret"].std(ddof=1) / np.sqrt(len(sub))) if len(sub) > 2 else 0
            net = edge - COST
            flag = "  <-- TRADEABLE" if net > 0 and t > 2.5 else ""
            print(f"  {str(name):22s} {len(sub):6d} {m:+9.4f}% {edge:+9.4f}% {t:+7.2f} {net:+11.4f}%{flag}")
        print()

    # Direction — the sample window is a bull market, so longs get free drift
    block("BY DIRECTION", [(d, g) for d, g in df.groupby("dir")])

    # Score deciles
    df["score_bucket"] = pd.cut(df["score"], [0, 65, 70, 75, 80, 85, 90, 101])
    block("BY SIGNAL SCORE", [(b, g) for b, g in df.groupby("score_bucket", observed=True)])

    # Setup
    block("BY SETUP", sorted([(s, g) for s, g in df.groupby("setup")], key=lambda kv: -len(kv[1]))[:10])

    # Volatility regime
    block("BY VOLATILITY REGIME", [(v, g) for v, g in df.groupby("vol")])

    # ADX / trend strength
    df["adx_bucket"] = pd.cut(df["adx"], [0, 20, 25, 30, 40, 100])
    block("BY ADX (trend strength)", [(b, g) for b, g in df.groupby("adx_bucket", observed=True)])

    # Relative volume
    df["rvol_bucket"] = pd.cut(df["rvol"], [0, 1.0, 1.5, 2.0, 3.0, 100])
    block("BY RELATIVE VOLUME", [(b, g) for b, g in df.groupby("rvol_bucket", observed=True)])

    # Time of day
    df["hour_bucket"] = pd.cut(df["hour"], [9, 10, 11, 12, 13, 14, 15.5])
    block("BY TIME OF DAY", [(b, g) for b, g in df.groupby("hour_bucket", observed=True)])

    best = df[(df["score"] >= 80) & (df["rvol"] >= 1.5) & (df["adx"] >= 25)]
    if len(best) > 30:
        m = best["ret"].mean(); t = m / (best["ret"].std(ddof=1)/np.sqrt(len(best)))
        print(f"COMBINED FILTER (score>=80, rvol>=1.5, adx>=25): n={len(best)} "
              f"mean={m:+.4f}% edge={m-drift:+.4f}% t={t:+.2f} net={m-drift-COST:+.4f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
