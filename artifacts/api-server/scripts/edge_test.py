"""Does the APEX entry signal have predictive edge at all?

Two controls, both on the same cached Angel data:

  TEST 1 — Forward-return edge
    Mean forward return in the signalled direction after an APEX signal bar,
    versus the unconditional mean forward return of every bar. If the signal
    carries information these differ materially (and the t-stat is large).

  TEST 2 — Random-entry control
    Run the identical fixed-target / fixed-stop exit machinery on RANDOM
    entry bars, matched for count and long/short mix. If APEX's win rate and
    expectancy match random, the entry logic is contributing nothing and no
    amount of target/stop tuning can create profit.
"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'python_scanner'))

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from apex_python_scanner import ApexConfig, ApexScanner

HERE = Path(__file__).resolve().parent
CACHE = HERE.parent / "python_scanner" / "angel_cache"
if not CACHE.exists():
    raise SystemExit(f"cache directory not found: {CACHE}")
RNG = np.random.default_rng(20260720)


def load_cached(interval: str, limit: int) -> dict[str, pd.DataFrame]:
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


def simulate_exit(frame: pd.DataFrame, entry_idx: int, is_long: bool,
                  tp_pct: float, sl_pct: float, max_bars: int = 200):
    """Walk bars forward from entry; stop-first ordering (conservative)."""
    if entry_idx + 1 >= len(frame):
        return None
    o = frame["open"].to_numpy(); h = frame["high"].to_numpy()
    l = frame["low"].to_numpy();  c = frame["close"].to_numpy()
    entry = float(o[entry_idx + 1])          # next-bar-open fill
    if not np.isfinite(entry) or entry <= 0:
        return None
    sign = 1.0 if is_long else -1.0
    tp = entry * (1 + sign * tp_pct / 100.0)
    sl = entry * (1 - sign * sl_pct / 100.0)
    for i in range(entry_idx + 1, min(entry_idx + 1 + max_bars, len(frame))):
        if is_long:
            if o[i] <= sl: return (o[i] - entry) / entry * 100.0
            if l[i] <= sl: return (sl - entry) / entry * 100.0
            if h[i] >= tp: return (tp - entry) / entry * 100.0
        else:
            if o[i] >= sl: return (entry - o[i]) / entry * 100.0
            if h[i] >= sl: return (entry - sl) / entry * 100.0
            if l[i] <= tp: return (entry - tp) / entry * 100.0
    last = float(c[min(entry_idx + max_bars, len(frame) - 1)])
    return sign * (last - entry) / entry * 100.0


def collect_signals(universe, interval: str):
    """Return per-symbol lists of (bar_index, is_long) for APEX signals."""
    cfg = ApexConfig(
        min_score=65.0 if interval == "15m" else 63.0,
        conflict_margin=15.0 if interval == "15m" else 14.0,
        use_htf=True, entry_delay_bars=1, realistic_fills=True,
        use_session=True, enforce_market_hours=True,
        keep_full_history=True, max_input_bars=0, strict_ohlcv=False,
        min_history_bars=210, fixed_sl_pct=0.5, force_fixed_sl=True,
        fixed_tp_pct=1.0, exit_at_t1=True, use_trail=False,
    )
    scanner = ApexScanner(cfg)
    out = {}
    for symbol, frame in universe.items():
        try:
            res = scanner.run_symbol(symbol, frame, asset_type="stock")
        except Exception:
            continue
        sig = res.frame["signal"]
        idx = np.flatnonzero(sig.isin(["BUY", "SELL"]).to_numpy())
        out[symbol] = [(int(i), sig.iat[int(i)] == "BUY") for i in idx]
    return out


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    for interval in ("15m", "1h"):
        universe = load_cached(interval, limit)
        if not universe:
            continue
        print(f"\n{'='*118}\n{interval.upper()} — {len(universe)} symbols\n{'='*118}")
        signals = collect_signals(universe, interval)
        horizon = 8 if interval == "15m" else 4

        # ---- TEST 1: forward-return edge -------------------------------
        sig_fwd, all_fwd = [], []
        for symbol, frame in universe.items():
            c = frame["close"].to_numpy()
            fwd = np.full(len(c), np.nan)
            fwd[:-horizon] = (c[horizon:] - c[:-horizon]) / c[:-horizon] * 100.0
            all_fwd.append(fwd[210:-horizon][np.isfinite(fwd[210:-horizon])])
            for i, is_long in signals.get(symbol, []):
                if i < len(fwd) and np.isfinite(fwd[i]):
                    sig_fwd.append(fwd[i] if is_long else -fwd[i])
        sig_fwd = np.array(sig_fwd); base = np.concatenate(all_fwd)
        # Baseline for a directional strategy: mean |unconditional| move is 0,
        # so compare the signal's directional return against 0 and against the
        # market's own drift.
        t_stat = sig_fwd.mean() / (sig_fwd.std(ddof=1) / np.sqrt(len(sig_fwd))) if len(sig_fwd) > 2 else 0.0
        print(f"TEST 1 — forward {horizon}-bar return in signal direction")
        print(f"  signals            n={len(sig_fwd):5d}  mean={sig_fwd.mean():+.4f}%  std={sig_fwd.std():.3f}%  t-stat={t_stat:+.2f}")
        print(f"  all bars (drift)   n={len(base):5d}  mean={base.mean():+.4f}%  std={base.std():.3f}%")
        print(f"  edge over drift    {sig_fwd.mean() - base.mean():+.4f}% per trade "
              f"({'SIGNIFICANT' if abs(t_stat) > 2.5 else 'NOT SIGNIFICANT (indistinguishable from noise)'})")

        # ---- TEST 2: random-entry control ------------------------------
        for tp, sl, tag in ((1.0, 0.5, "1.00%/0.50% (1:2)"), (0.75, 0.375, "0.75%/0.375% (1:2)")):
            apex_r, rand_r = [], []
            for symbol, frame in universe.items():
                sigs = signals.get(symbol, [])
                for i, is_long in sigs:
                    r = simulate_exit(frame, i, is_long, tp, sl)
                    if r is not None: apex_r.append(r)
                # matched random entries: same count, same long/short mix
                if sigs:
                    longs = sum(1 for _, L in sigs if L)
                    lo, hi = 210, len(frame) - 2
                    if hi > lo:
                        picks = RNG.integers(lo, hi, size=len(sigs))
                        for k, i in enumerate(picks):
                            r = simulate_exit(frame, int(i), k < longs, tp, sl)
                            if r is not None: rand_r.append(r)
            a, rd = np.array(apex_r), np.array(rand_r)
            if len(a) and len(rd):
                print(f"\nTEST 2 — random-entry control @ {tag}")
                print(f"  APEX signals   n={len(a):5d}  WR={(a>0).mean()*100:5.1f}%  expectancy={a.mean():+.4f}%/trade")
                print(f"  RANDOM entries n={len(rd):5d}  WR={(rd>0).mean()*100:5.1f}%  expectancy={rd.mean():+.4f}%/trade")
                diff = a.mean() - rd.mean()
                se = np.sqrt(a.var(ddof=1)/len(a) + rd.var(ddof=1)/len(rd))
                t = diff / se if se > 0 else 0.0
                print(f"  APEX minus random: {diff:+.4f}%/trade  t={t:+.2f}  -> "
                      f"{'REAL EDGE' if t > 2.5 else 'NO MEASURABLE EDGE'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
