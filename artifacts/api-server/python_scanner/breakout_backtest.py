"""Verify the Breakout-only finding through real TP/SL trade simulation.

Stratification showed the pooled signal has no tradeable edge, but the
'Breakout' setup carried +0.28% over drift (t=7.4) — enough to clear the
~0.18% round-trip cost. Forward-return edge does not automatically survive
a stop-and-target exit path, so this re-tests it as actual trades.

Compares, net of costs:
    ALL signals   vs   BREAKOUT only   vs   BREAKOUT + filters
across several target/stop geometries.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from apex_python_scanner import ApexConfig, ApexScanner

CACHE = Path(__file__).resolve().parent / "angel_cache"
COST = 0.182  # charges + slippage, round trip %


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


def simulate(frame_arrays, entry_idx, is_long, tp_pct, sl_pct, max_bars=200):
    o, h, l, c = frame_arrays
    if entry_idx + 1 >= len(o):
        return None
    entry = float(o[entry_idx + 1])
    if not np.isfinite(entry) or entry <= 0:
        return None
    sign = 1.0 if is_long else -1.0
    tp = entry * (1 + sign * tp_pct / 100.0)
    sl = entry * (1 - sign * sl_pct / 100.0)
    for i in range(entry_idx + 1, min(entry_idx + 1 + max_bars, len(o))):
        if is_long:
            if o[i] <= sl: return (o[i] - entry) / entry * 100.0, i - entry_idx
            if l[i] <= sl: return (sl - entry) / entry * 100.0, i - entry_idx
            if h[i] >= tp: return (tp - entry) / entry * 100.0, i - entry_idx
        else:
            if o[i] >= sl: return (entry - o[i]) / entry * 100.0, i - entry_idx
            if h[i] >= sl: return (entry - sl) / entry * 100.0, i - entry_idx
            if l[i] <= tp: return (entry - tp) / entry * 100.0, i - entry_idx
    last = float(c[min(entry_idx + max_bars, len(o) - 1)])
    return sign * (last - entry) / entry * 100.0, max_bars


def report(tag, rets, bars):
    if len(rets) < 20:
        print(f"  {tag:38s} n={len(rets):5d}  (too few)")
        return None
    net = np.array(rets) - COST
    wins, losses = net[net > 0], -net[net <= 0]
    gp, gl = wins.sum(), losses.sum()
    pf = gp / gl if gl > 1e-9 else float("inf")
    exp = net.mean()
    se = net.std(ddof=1) / np.sqrt(len(net))
    t = exp / se if se > 0 else 0
    verdict = "PROFITABLE" if exp > 0 and t > 2.0 else ("marginal" if exp > 0 else "LOSS")
    print(f"  {tag:38s} n={len(net):5d}  WR={(net>0).mean()*100:5.1f}%  "
          f"PF={pf:5.2f}  exp={exp:+.4f}%  t={t:+5.2f}  total={net.sum():+8.1f}%  "
          f"avgbars={np.mean(bars):5.1f}  [{verdict}]")
    return exp


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    universe = load_cached("15m", limit)
    cfg = ApexConfig(
        min_score=60.0, conflict_margin=12.0, use_htf=True, entry_delay_bars=1,
        realistic_fills=True, use_session=True, enforce_market_hours=True,
        keep_full_history=True, max_input_bars=0, strict_ohlcv=False,
        min_history_bars=210, fixed_sl_pct=0.5, force_fixed_sl=True,
        fixed_tp_pct=1.0, exit_at_t1=True, use_trail=False,
    )
    scanner = ApexScanner(cfg)

    # Collect every signal once with its context, then re-simulate per geometry.
    catalogue = []   # (arrays, idx, is_long, setup, rvol, hour)
    for symbol, frame in universe.items():
        try:
            res = scanner.run_symbol(symbol, frame, asset_type="stock")
        except Exception:
            continue
        f = res.frame
        arrays = (f["open"].to_numpy(), f["high"].to_numpy(), f["low"].to_numpy(), f["close"].to_numpy())
        sig = f["signal"].to_numpy()
        setup = f["setup"].to_numpy()
        rvol = f["relative_volume"].to_numpy()
        hours = (f.index.hour + f.index.minute / 60.0).to_numpy()
        for i in np.flatnonzero(pd.Series(sig).isin(["BUY", "SELL"]).to_numpy()):
            catalogue.append((arrays, int(i), sig[i] == "BUY", str(setup[i]),
                              float(rvol[i]) if np.isfinite(rvol[i]) else 0.0, float(hours[i])))

    print(f"15m — {len(universe)} symbols, {len(catalogue)} signals, cost {COST}% round trip\n")

    geometries = [
        ("0.75% tgt / 0.375% SL (1:2)", 0.75, 0.375),
        ("1.00% tgt / 0.50% SL  (1:2)", 1.00, 0.50),
        ("1.50% tgt / 0.75% SL  (1:2)", 1.50, 0.75),
        ("2.00% tgt / 1.00% SL  (1:2)", 2.00, 1.00),
        ("1.00% tgt / 0.75% SL (1:1.3)", 1.00, 0.75),
    ]

    cohorts = {
        "ALL signals": lambda s: True,
        "LONG only": lambda s: s[2],
        "BREAKOUT only": lambda s: s[3] == "Breakout",
        "BREAKOUT + rvol>=2": lambda s: s[3] == "Breakout" and s[4] >= 2.0,
        "BREAKOUT + afternoon(>=13h)": lambda s: s[3] == "Breakout" and s[5] >= 13.0,
        "BREAKOUT + rvol>=2 + aft": lambda s: s[3] == "Breakout" and s[4] >= 2.0 and s[5] >= 13.0,
    }

    for label, tp, sl in geometries:
        print(f"--- {label} " + "-" * (78 - len(label)))
        for cname, pred in cohorts.items():
            rets, bars = [], []
            for s in catalogue:
                if not pred(s):
                    continue
                out = simulate(s[0], s[1], s[2], tp, sl)
                if out is not None:
                    rets.append(out[0]); bars.append(out[1])
            report(cname, rets, bars)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
