"""Full system audit: run backtest + edge gate + DB analysis with the fixed code.
Compare results against the original audit baseline numbers."""
from __future__ import annotations
import sys, os, json, math, sqlite3
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.getcwd(), 'artifacts', 'api-server', 'python_scanner'))

from apex_python_scanner import ApexConfig, ApexScanner
from backtest_api import run_backtest
from edge_gate import evaluate_edge
from pathlib import Path

DB = Path(os.getcwd()) / "artifacts" / "api-server" / "python_scanner" / "apex_trading.db"

BASELINE = {
    "win_rate": 34.26,
    "mean_return": -0.53,
    "edge_over_random": -0.08,
    "t_stat": -0.42,
    "sl1_to_tp1_ratio": 4.8,
    "15m_wr": 31.5,
    "1h_wr": 36.0,
    "4h_wr": 49.0,
}

print("=" * 72)
print("APEX FULL SYSTEM AUDIT — POST-FIX ANALYSIS")
print("=" * 72)

# ─── Part 1: Config Verification ───
print("\n┌─────────────────────────────────────────┐")
print("│ PART 1: CONFIGURATION VERIFICATION      │")
print("└─────────────────────────────────────────┘")
cfg = ApexConfig(timeframe="15m")
checks = {
    "min_adx": (cfg.min_adx, 25.0, ">="),
    "circuit_pause_bars": (cfg.circuit_pause_bars, 999, "=="),
}
for name, (actual, expected, op) in checks.items():
    ok = actual >= expected if op == ">=" else actual == expected
    status = "✓ PASS" if ok else "✗ FAIL"
    print(f"  {status}: {name} = {actual} (expected {op} {expected})")

# Check SL cap
dummy_row = pd.Series({'atr': 50.0, 'high_volatility': 1.0, 'low_volatility': 0.0, 'is_trending': 1.0})
from apex_python_scanner import calculate_stops
sl1, _, _, _ = calculate_stops(True, 1000.0, 900.0, 1100.0, dummy_row, cfg)
sl_pct = (1000.0 - sl1) / 1000.0 * 100
print(f"  {'✓ PASS' if sl_pct > 2.5 else '✗ FAIL'}: SL cap allows {sl_pct:.1f}% (was max 2.0%)")

# ─── Part 2: Run Full Backtest (15m) ───
print("\n┌─────────────────────────────────────────┐")
print("│ PART 2: FULL 15m BACKTEST (60 days)     │")
print("└─────────────────────────────────────────┘")
print("  Running backtest on all cached symbols... (this may take a minute)")

bt_15m = run_backtest(
    symbol=None,
    timeframe="15m",
    days=60,
    min_score=45.0,
    conflict_margin=10.0,
    atr_mult=2.0,
    exit_at_t1=False,
    slippage_pct=0.05,
    cost_pct=0.182,
)

s = bt_15m["summary"]
print(f"\n  {'Metric':<30s} {'OLD':>10s} {'NEW':>10s} {'CHANGE':>12s}")
print(f"  {'─'*62}")
print(f"  {'Total Trades':<30s} {'~1768':>10s} {s['total_trades']:>10d}")
print(f"  {'Win Rate %':<30s} {BASELINE['15m_wr']:>10.1f} {s['win_rate_pct']:>10.1f} {s['win_rate_pct'] - BASELINE['15m_wr']:>+11.1f}")
print(f"  {'Avg PnL %/trade':<30s} {BASELINE['mean_return']:>10.2f} {s['avg_pnl_pct']:>10.4f}")
print(f"  {'Total PnL %':<30s} {'---':>10s} {s['total_pnl_pct']:>10.2f}")
print(f"  {'Profit Factor':<30s} {'<1.0':>10s} {s['profit_factor']:>10.2f}")
print(f"  {'Sharpe Ratio':<30s} {'<0':>10s} {s['sharpe_ratio']:>10.2f}")
print(f"  {'Max Drawdown %':<30s} {'---':>10s} {s['max_drawdown_pct']:>10.2f}")
print(f"  {'Symbols Tested':<30s} {'---':>10s} {s['symbols_tested']:>10d}")
print(f"  {'Symbols With Trades':<30s} {'---':>10s} {s['symbols_with_trades']:>10d}")

# Direction breakdown
db = bt_15m.get("direction_breakdown", {})
if db:
    print(f"\n  Direction Breakdown:")
    for d_name, d_stats in db.items():
        print(f"    {d_name.upper()}: {d_stats['count']} trades, {d_stats['win_rate_pct']:.1f}% WR, {d_stats['avg_pnl_pct']:+.4f}%/trade")

# Setup breakdown
sb = bt_15m.get("setup_breakdown", [])
if sb:
    print(f"\n  Setup Breakdown:")
    for entry in sb:
        print(f"    {entry['setup']:<20s}: {entry['count']:>4d} trades, {entry['win_rate_pct']:>5.1f}% WR, {entry['avg_pnl_pct']:>+8.4f}%/trade")

# Score bands
bands = bt_15m.get("score_bands", [])
if bands:
    print(f"\n  Score Band Analysis:")
    for b in bands:
        print(f"    {b['band']:<10s}: {b['count']:>5d} trades, {b['win_rate_pct']:>5.1f}% WR, {b['avg_pnl_pct']:>+8.4f}%/trade")

# ─── Part 3: Run Full Backtest (1h) ───
print("\n┌─────────────────────────────────────────┐")
print("│ PART 3: FULL 1h BACKTEST (60 days)      │")
print("└─────────────────────────────────────────┘")
print("  Running 1h backtest...")

bt_1h = run_backtest(
    symbol=None,
    timeframe="1h",
    days=60,
    min_score=45.0,
    conflict_margin=10.0,
    atr_mult=2.0,
    exit_at_t1=False,
    slippage_pct=0.05,
    cost_pct=0.182,
)
s1h = bt_1h["summary"]
print(f"  Trades: {s1h['total_trades']}, Win Rate: {s1h['win_rate_pct']:.1f}%, "
      f"Avg PnL: {s1h['avg_pnl_pct']:+.4f}%, PF: {s1h['profit_factor']:.2f}")

# ─── Part 4: Edge Gate ───
print("\n┌─────────────────────────────────────────┐")
print("│ PART 4: EDGE GATE (from Trade DB)       │")
print("└─────────────────────────────────────────┘")
if DB.exists():
    report = evaluate_edge(DB)
    print(f"  Trades:               {report.trades}")
    print(f"  Trading Days:         {report.days}")
    print(f"  APEX Mean (net):      {report.apex_mean_pct:+.4f}%/trade")
    print(f"  Random Control (net): {report.random_mean_pct:+.4f}%/trade")
    print(f"  Edge Over Random:     {report.edge_pct:+.4f}% (was {BASELINE['edge_over_random']:+.2f}%)")
    print(f"  Clustered t-stat:     {report.edge_t_stat:+.3f} (was {BASELINE['t_stat']:+.2f})")
    print(f"  95% CI:               [{report.edge_ci_low:+.4f}, {report.edge_ci_high:+.4f}]")
    print(f"  Verdict:              {'PASS' if report.passes else 'FAIL'}")
    for r in report.reasons:
        print(f"    - {r}")
else:
    print("  [SKIP] No apex_trading.db found — edge gate needs live trade data.")

# ─── Part 5: DB Trade Analysis ───
print("\n┌─────────────────────────────────────────┐")
print("│ PART 5: TRADE DATABASE ANALYSIS         │")
print("└─────────────────────────────────────────┘")
if DB.exists():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT timeframe, direction, status, pnl, exit_reason, entry_time, exit_time "
        "FROM trades WHERE status='CLOSED' AND pnl IS NOT NULL"
    ).fetchall()
    conn.close()

    if rows:
        df = pd.DataFrame([dict(r) for r in rows])
        df['pnl'] = df['pnl'].astype(float)
        df['won'] = df['pnl'] > 0

        # Overall
        total = len(df)
        wins = df['won'].sum()
        wr = wins / total * 100
        print(f"  Total Closed Trades: {total}")
        print(f"  Overall Win Rate:    {wr:.1f}% (was {BASELINE['win_rate']:.1f}%)")
        print(f"  Mean PnL:            {df['pnl'].mean():+.4f}%")

        # By timeframe
        print(f"\n  By Timeframe:")
        for tf, grp in df.groupby('timeframe'):
            tw = grp['won'].sum()
            tt = len(grp)
            print(f"    {tf}: {tt} trades, {tw/tt*100:.1f}% WR, {grp['pnl'].mean():+.4f}% avg")

        # By exit reason
        print(f"\n  Top Exit Reasons:")
        for reason, grp in df.groupby('exit_reason').size().sort_values(ascending=False).head(10).items():
            sub = df[df['exit_reason'] == reason]
            print(f"    {reason:<40s}: {grp:>4d} trades, {sub['won'].mean()*100:>5.1f}% WR")
    else:
        print("  No closed trades found in DB.")
else:
    print("  [SKIP] No database found.")

# ─── Final Verdict ───
print("\n" + "=" * 72)
print("FINAL VERDICT")
print("=" * 72)

improved = s['win_rate_pct'] > BASELINE['15m_wr']
if improved:
    delta = s['win_rate_pct'] - BASELINE['15m_wr']
    print(f"  ✓ 15m Win Rate IMPROVED: {BASELINE['15m_wr']:.1f}% → {s['win_rate_pct']:.1f}% (+{delta:.1f}pp)")
else:
    delta = s['win_rate_pct'] - BASELINE['15m_wr']
    print(f"  ✗ 15m Win Rate: {BASELINE['15m_wr']:.1f}% → {s['win_rate_pct']:.1f}% ({delta:+.1f}pp)")

if s['profit_factor'] > 1.0:
    print(f"  ✓ Profit Factor > 1.0: {s['profit_factor']:.2f}")
else:
    print(f"  ✗ Profit Factor < 1.0: {s['profit_factor']:.2f}")

if s['avg_pnl_pct'] > 0:
    print(f"  ✓ Positive Expectancy: {s['avg_pnl_pct']:+.4f}%/trade")
else:
    print(f"  ✗ Negative Expectancy: {s['avg_pnl_pct']:+.4f}%/trade")

trade_count_change = "FEWER" if s['total_trades'] < 1768 else "MORE"
print(f"  ℹ Trade Count: {s['total_trades']} ({trade_count_change} than baseline 1768 — expected with stricter filters)")
print("=" * 72)
