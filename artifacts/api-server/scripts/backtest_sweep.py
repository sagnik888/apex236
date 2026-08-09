"""Parameter sweep over cached Angel history with a real cost model.

Answers: does an intraday 15m/1h strategy targeting 0.75-1% moves at a
1:2 reward:risk actually make money AFTER Indian intraday costs?

Cost model (NSE equity intraday / MIS, Zerodha-style, per round trip):
  brokerage  0.03% per side capped at Rs.20      -> ~0.040% on a Rs.1L trade
  STT        0.025% on the SELL side only        -> 0.025%
  exchange   0.00297% per side                   -> 0.00594%
  stamp duty 0.003% on the BUY side              -> 0.003%
  SEBI       0.0001% per side                    -> 0.0002%
  GST        18% on (brokerage + exchange + SEBI)-> ~0.0083%
                                            TOTAL ~0.082% of turnover
Slippage is modelled separately and dominates: entries/exits are market
orders on a 236-name universe including midcaps.
"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'python_scanner'))

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from apex_python_scanner import ApexConfig, ApexScanner

HERE = Path(__file__).resolve().parent
CACHE = HERE.parent / "python_scanner" / "angel_cache"
if not CACHE.exists():
    raise SystemExit(f"cache directory not found: {CACHE}")

STATUTORY_ROUND_TRIP_PCT = 0.082      # charges, see docstring
SLIPPAGE_PER_SIDE_PCT = 0.05          # base case; swept below
EOD_SQUARE_OFF = pd.Timestamp("15:15").time()


def load_cached(interval: str, limit: int | None = None) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for path in sorted(CACHE.glob(f"*__{interval}.pkl")):
        symbol = path.name.split("__")[0]
        try:
            frame = pd.read_pickle(path)
            if isinstance(frame, pd.DataFrame) and len(frame) > 250:
                frames[symbol] = frame
        except Exception:
            continue
        if limit and len(frames) >= limit:
            break
    return frames


def make_config(tp_pct: float, sl_pct: float, interval: str, intraday_only: bool) -> ApexConfig:
    """Deterministic fixed-% stop and target => exact reward:risk."""
    return ApexConfig(
        min_score=65.0 if interval == "15m" else 63.0,
        conflict_margin=15.0 if interval == "15m" else 14.0,
        use_htf=True,
        entry_delay_bars=1,            # honest next-bar fills
        realistic_fills=True,
        allow_entry_on_last_bar=True,
        use_session=True,
        enforce_market_hours=True,
        fixed_sl_pct=sl_pct,
        force_fixed_sl=True,           # guarantee the ratio in every regime
        fixed_tp_pct=tp_pct,
        exit_at_t1=True,               # book the whole position at target
        use_trail=False,
        keep_full_history=True,
        max_input_bars=0,
        strict_ohlcv=False,
        min_history_bars=210,
    )


def apply_costs(trades, slippage_side: float) -> dict:
    """Convert gross trade PnL% into net after charges + slippage."""
    round_trip = STATUTORY_ROUND_TRIP_PCT + 2 * slippage_side
    gross = np.array([t.pnl_pct for t in trades], dtype=float)
    net = gross - round_trip
    if len(net) == 0:
        return {"n": 0}
    wins_g, wins_n = gross[gross > 0], net[net > 0]
    losses_n = -net[net <= 0]
    gp, gl = float(wins_n.sum()), float(losses_n.sum())
    same_day = sum(
        1 for t in trades
        if pd.Timestamp(t.entry_time).date() == pd.Timestamp(t.exit_time).date()
    )
    return {
        "n": len(net),
        "wr_gross": float((gross > 0).mean() * 100),
        "wr_net": float((net > 0).mean() * 100),
        "avg_win_net": float(wins_n.mean()) if len(wins_n) else 0.0,
        "avg_loss_net": float(losses_n.mean()) if len(losses_n) else 0.0,
        "pf_net": (gp / gl) if gl > 1e-9 else float("inf") if gp > 0 else 0.0,
        "expectancy_net": float(net.mean()),
        "total_net": float(net.sum()),
        "round_trip_cost": round_trip,
        "same_day_pct": same_day / len(net) * 100,
        "avg_bars": float(np.mean([t.bars_held for t in trades])),
    }


def run_config(universe: dict[str, pd.DataFrame], cfg: ApexConfig, label: str) -> list:
    scanner = ApexScanner(cfg)
    all_trades = []
    for symbol, frame in universe.items():
        try:
            result = scanner.run_symbol(symbol, frame, asset_type="stock")
            all_trades.extend(result.trades)
        except Exception:
            continue
    return all_trades


def fmt_row(label: str, s: dict) -> str:
    if not s.get("n"):
        return f"  {label:34s} — no trades"
    pf = s["pf_net"]
    pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
    verdict = "PROFIT" if s["total_net"] > 0 else "LOSS"
    return (
        f"  {label:34s} n={s['n']:5d}  WRgross={s['wr_gross']:5.1f}%  WRnet={s['wr_net']:5.1f}%  "
        f"avgW={s['avg_win_net']:+.2f}%  avgL={-s['avg_loss_net']:+.2f}%  PF={pf_s:>5s}  "
        f"exp={s['expectancy_net']:+.3f}%/trade  total={s['total_net']:+8.1f}%  [{verdict}]"
    )


def main() -> int:
    symbol_limit = int(sys.argv[1]) if len(sys.argv) > 1 else 80
    print(f"Loading cached Angel history (limit {symbol_limit} symbols/interval)…", flush=True)
    universes = {
        "15m": load_cached("15m", symbol_limit),
        "1h": load_cached("1h", symbol_limit),
    }
    for interval, u in universes.items():
        if u:
            spans = [f.index[-1] - f.index[0] for f in u.values()]
            bars = int(np.mean([len(f) for f in u.values()]))
            print(f"  {interval}: {len(u)} symbols, ~{bars} bars each, ~{np.mean([s.days for s in spans]):.0f} days")

    # Target / stop grid. Ratio is target:stop (reward:risk).
    grid = [
        ("0.75% tgt / 0.375% SL", 0.75, 0.375),   # user's 1:2 at 0.75%
        ("1.00% tgt / 0.50% SL",  1.00, 0.50),    # user's 1:2 at 1.0%
        ("1.50% tgt / 0.75% SL",  1.50, 0.75),    # 1:2, wider
        ("2.00% tgt / 1.00% SL",  2.00, 1.00),    # 1:2, widest
        ("0.75% tgt / 0.75% SL",  0.75, 0.75),    # 1:1 control
        ("1.00% tgt / 1.00% SL",  1.00, 1.00),    # 1:1 control
    ]

    results: dict[tuple[str, str], dict] = {}
    for interval, universe in universes.items():
        if not universe:
            continue
        print(f"\n{'='*140}\n{interval.upper()} — {len(universe)} symbols "
              f"(costs: {STATUTORY_ROUND_TRIP_PCT}% charges + {2*SLIPPAGE_PER_SIDE_PCT}% slippage "
              f"= {STATUTORY_ROUND_TRIP_PCT + 2*SLIPPAGE_PER_SIDE_PCT}% round trip)\n{'='*140}")
        for label, tp, sl in grid:
            t0 = time.monotonic()
            cfg = make_config(tp, sl, interval, intraday_only=True)
            trades = run_config(universe, cfg, label)
            stats = apply_costs(trades, SLIPPAGE_PER_SIDE_PCT)
            results[(interval, label)] = stats
            print(fmt_row(label, stats), f" ({time.monotonic()-t0:.0f}s)", flush=True)

    # Slippage sensitivity on the user's headline config
    print(f"\n{'='*140}\nSLIPPAGE SENSITIVITY — 1.00% tgt / 0.50% SL (the 1:2 proposal)\n{'='*140}")
    for interval, universe in universes.items():
        if not universe:
            continue
        cfg = make_config(1.00, 0.50, interval, intraday_only=True)
        trades = run_config(universe, cfg, "sens")
        for slip in (0.0, 0.02, 0.05, 0.10):
            s = apply_costs(trades, slip)
            print(fmt_row(f"{interval} slippage {slip*2:.2f}% RT", s))

    print("\nNOTE: gross win rate is what the strategy achieves; net applies the round-trip cost")
    print("to every trade. 'same_day_pct' below shows how many trades close within the session.")
    for (interval, label), s in results.items():
        if s.get("n"):
            print(f"  {interval:3s} {label:28s} same-day={s['same_day_pct']:5.1f}%  avg_bars={s['avg_bars']:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
