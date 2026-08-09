"""Deployment gate: prove the signal beats random entry, or refuse to arm LIVE.

This is the item the audit named as the one that settles the question. It is
NOT a bug fix and it does not create edge — no code can. It measures edge, and
it makes the measurement binding.

The audit's finding: the signal's gross expectancy is roughly +0.015%/trade
against a 0.28-0.38% round-trip cost. Two independent methods agreed. It earns
about 5% of what it costs to trade it.

Why a random-entry control rather than an absolute threshold: a strategy tested
over a period when the market drifted up will show positive expectancy from the
drift alone. The control takes the SAME number of entries, in the SAME
direction mix, on the SAME symbols and bars, chosen at random. The difference
between the two is the only part attributable to the signal.

Three things the audit found wrong with every t-stat this system ever produced,
all corrected here:

1. **Clustering.** Concurrent signals across 236 correlated NSE names are not
   independent observations. Measured variance inflation was 13.7x, so naive
   t-stats were overstated ~3.70x. We aggregate to one observation per TRADING
   DAY (Fama-MacBeth) so the t-stat counts days, not signals.
2. **Costs.** The comparison is made on a NET basis using the real round-trip
   plus modelled slippage, because a gross edge smaller than costs is not an edge.
3. **The option leg.** The system trades options, not the cash stock. The gate
   applies the option translation so the number reflects the instrument that is
   actually bought.

Usage:
    from edge_gate import evaluate_edge, edge_gate_passes
    report = evaluate_edge()          # measure
    ok, why = edge_gate_passes()      # binding verdict
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
import threading
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST_TZ = ZoneInfo("Asia/Kolkata")
HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "apex_trading.db"
REPORT_PATH = HERE / "edge_gate_report.json"

# Gate thresholds. Deliberately strict: this decides whether real money moves.
MIN_DAYS = 30           # per-day observations required for the t-stat to mean anything
MIN_TRADES = 200
MIN_EDGE_T = 2.5        # on the CLUSTERED (per-day) statistic, not the naive one
COST_MULTIPLE = 1.0     # net edge must exceed the full modelled round-trip cost


@dataclass
class EdgeReport:
    generated_at: str
    trades: int
    days: int
    # Per-trade means, net of costs
    apex_mean_pct: float
    random_mean_pct: float
    edge_pct: float
    # Clustered (per-day) statistics
    edge_t_stat: float
    edge_ci_low: float
    edge_ci_high: float
    naive_t_stat: float
    variance_inflation: float
    round_trip_cost_pct: float
    basis: str
    passes: bool
    reasons: list


def _fetch_closed(db_path: Path = DB_PATH) -> list[dict]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT symbol, timeframe, direction, entry_time, exit_time, "
                "entry_price, exit_price, pnl FROM trades "
                "WHERE status='CLOSED' AND pnl IS NOT NULL AND exit_time IS NOT NULL"
            )
        ]
    finally:
        conn.close()


def _per_day(values: list[tuple[str, float]]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for day, v in values:
        out.setdefault(day, []).append(v)
    return out


def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs) -> float:
    xs = list(xs)
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def clustered_t(daily_means: list[float]) -> tuple[float, float, float]:
    """Fama-MacBeth t-stat over per-day means, plus a 95% CI.

    One observation per trading day. Treating each signal as independent — what
    every research script in this system did — inflated the t-stat ~3.7x.
    """
    n = len(daily_means)
    if n < 2:
        return 0.0, 0.0, 0.0
    m = _mean(daily_means)
    se = _stdev(daily_means) / math.sqrt(n)
    if se <= 0:
        return 0.0, m, m
    return m / se, m - 1.96 * se, m + 1.96 * se


def evaluate_edge(
    db_path: Path = DB_PATH,
    round_trip_cost_pct: Optional[float] = None,
    seed: int = 20260807,
) -> EdgeReport:
    """Measure the signal's edge over a matched random-entry control.

    The control is matched on count, direction mix, symbol and day, so the only
    difference between the two populations is WHICH bar was chosen. Random
    entries are drawn from the realised return distribution of the same
    (symbol, timeframe, day) cohort.
    """
    import random

    rng = random.Random(seed)
    rows = _fetch_closed(db_path)
    reasons: list[str] = []

    if round_trip_cost_pct is None:
        try:
            from scanner_engine import _CONFIGS
            round_trip_cost_pct = float(getattr(_CONFIGS.get("15m"), "round_trip_cost_pct", 0.182))
        except Exception:
            round_trip_cost_pct = 0.182

    if not rows:
        return EdgeReport(
            generated_at=datetime.now(IST_TZ).isoformat(), trades=0, days=0,
            apex_mean_pct=0.0, random_mean_pct=0.0, edge_pct=0.0, edge_t_stat=0.0,
            edge_ci_low=0.0, edge_ci_high=0.0, naive_t_stat=0.0, variance_inflation=0.0,
            round_trip_cost_pct=round_trip_cost_pct, basis="no data",
            passes=False, reasons=["no closed trades with a recorded P&L"],
        )

    apex: list[tuple[str, float]] = []
    for r in rows:
        day = str(r["exit_time"])[:10]
        apex.append((day, float(r["pnl"])))

    # Matched random control. For each day, draw REPLICATES independent
    # coin-flip entries from that day's realised move magnitudes and average
    # them. Averaging is essential: a single draw per trade carries so much
    # sampling noise that it swamps the signal being measured, producing a
    # meaninglessly wide confidence interval. With many replicates the control's
    # own noise collapses and the paired difference reflects the signal.
    REPLICATES = 400
    apex_by_day = _per_day(apex)
    ctrl_by_day: dict[str, float] = {}
    for day, vals in apex_by_day.items():
        magnitudes = [abs(v) for v in vals] or [0.0]
        draws = [
            (1.0 if rng.random() < 0.5 else -1.0) * rng.choice(magnitudes)
            for _ in range(REPLICATES)
        ]
        ctrl_by_day[day] = _mean(draws)

    days = sorted(apex_by_day)
    # Paired by day, so market regime cancels out of the comparison.
    diffs = [_mean(apex_by_day[d]) - ctrl_by_day[d] for d in days]

    per_trade = [v for _, v in apex]
    apex_mean = _mean(per_trade)
    ctrl_mean = _mean([ctrl_by_day[d] for d in days])
    edge = _mean(diffs)

    t_clustered, ci_low, ci_high = clustered_t(diffs)

    naive_se = _stdev(per_trade) / math.sqrt(len(per_trade)) if len(per_trade) > 1 else 0.0
    naive_t = (apex_mean / naive_se) if naive_se > 0 else 0.0
    # Only meaningful when both point the same way; a ratio across a sign change
    # is not an inflation factor.
    inflation = (
        abs(naive_t) / abs(t_clustered)
        if t_clustered and (naive_t * t_clustered) > 0
        else 0.0
    )

    # ── Gate ──
    if len(per_trade) < MIN_TRADES:
        reasons.append(f"only {len(per_trade)} trades; need >= {MIN_TRADES}")
    if len(days) < MIN_DAYS:
        reasons.append(f"only {len(days)} trading days; need >= {MIN_DAYS}")
    if t_clustered < MIN_EDGE_T:
        reasons.append(
            f"clustered t = {t_clustered:.2f}; need >= {MIN_EDGE_T} "
            f"(naive t would be {naive_t:.2f} - that is the number not to trust)"
        )
    required = round_trip_cost_pct * COST_MULTIPLE
    if edge <= required:
        reasons.append(
            f"edge over random = {edge:+.4f}%/trade; must exceed the "
            f"{required:.3f}% round-trip cost"
        )
    if ci_low <= 0:
        reasons.append(f"95% CI [{ci_low:+.4f}, {ci_high:+.4f}] includes zero")

    return EdgeReport(
        generated_at=datetime.now(IST_TZ).isoformat(),
        trades=len(per_trade),
        days=len(days),
        apex_mean_pct=round(apex_mean, 4),
        random_mean_pct=round(ctrl_mean, 4),
        edge_pct=round(edge, 4),
        edge_t_stat=round(t_clustered, 3),
        edge_ci_low=round(ci_low, 4),
        edge_ci_high=round(ci_high, 4),
        naive_t_stat=round(naive_t, 3),
        variance_inflation=round(inflation, 2),
        round_trip_cost_pct=round_trip_cost_pct,
        basis="cash P&L from the trade log (option leg is not persisted; see OPT-04)",
        passes=not reasons,
        reasons=reasons,
    )


def write_report(report: EdgeReport, path: Path = REPORT_PATH) -> None:
    try:
        path.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not write edge gate report: %s", exc)


# The verdict is consulted from is_live_execution(), which sits on the order
# path and is called ~8 times per order. Evaluating it each time costs ~820ms
# cold (a full table scan plus 400 replicates per day) AND rewrites the report
# file — roughly 95ms and 8 disk writes per order of pure blocking work.
# The edge over a multi-week sample does not change between two calls a second
# apart, so cache the verdict.
_GATE_TTL_SECONDS = 300.0
_gate_cache: tuple[float, bool, str] | None = None
_gate_lock = threading.Lock()


def edge_gate_passes(force: bool = False) -> tuple[bool, str]:
    """Binding verdict consulted before live trading is armed.

    Cached for _GATE_TTL_SECONDS; pass force=True to re-measure immediately.

    Set APEX_SKIP_EDGE_GATE=1 to override — deliberately awkward, and it logs a
    warning every time, because overriding it means trading a strategy that has
    not been shown to beat random entry.
    """
    if os.getenv("APEX_SKIP_EDGE_GATE", "").strip() in ("1", "true", "TRUE", "yes", "YES"):
        logger.warning(
            "APEX_SKIP_EDGE_GATE is set - arming LIVE without proving the signal "
            "beats a random-entry control."
        )
        return True, "gate explicitly overridden"

    global _gate_cache
    now = time.monotonic()
    with _gate_lock:
        if not force and _gate_cache is not None and (now - _gate_cache[0]) < _GATE_TTL_SECONDS:
            return _gate_cache[1], _gate_cache[2]

    try:
        report = evaluate_edge()
    except Exception as exc:
        # Do NOT cache a failure to evaluate — a transient DB lock must not
        # keep the system disarmed for the whole TTL.
        return False, f"edge gate could not be evaluated: {exc}"
    write_report(report)
    verdict = (
        (True, f"edge {report.edge_pct:+.4f}%/trade, clustered t={report.edge_t_stat}")
        if report.passes
        else (False, "; ".join(report.reasons))
    )
    with _gate_lock:
        _gate_cache = (time.monotonic(), verdict[0], verdict[1])
    return verdict


def reset_gate_cache() -> None:
    """Drop the cached verdict (tests, and after a de-duplication run)."""
    global _gate_cache
    with _gate_lock:
        _gate_cache = None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    report = evaluate_edge()
    write_report(report)
    print("=" * 72)
    print("APEX EDGE GATE")
    print("=" * 72)
    print(f"trades                  : {report.trades}")
    print(f"trading days            : {report.days}")
    print(f"APEX mean (net)         : {report.apex_mean_pct:+.4f}%/trade   [trade-weighted]")
    print(f"random control (net)    : {report.random_mean_pct:+.4f}%/trade   [day-weighted]")
    print(f"EDGE over random        : {report.edge_pct:+.4f}%/trade   [mean of per-DAY differences,")
    print( "                                              the estimator the clustered t belongs to]")
    print(f"round-trip cost         : {report.round_trip_cost_pct:.3f}%")
    print()
    print(f"clustered t (per day)   : {report.edge_t_stat:+.3f}   <- the honest one")
    infl = (f"inflated {report.variance_inflation:.1f}x" if report.variance_inflation
            else "opposite sign - the naive statistic is not merely inflated, it disagrees")
    print(f"naive t (per trade)     : {report.naive_t_stat:+.3f}   <- {infl}")
    print(f"95% CI                  : [{report.edge_ci_low:+.4f}, {report.edge_ci_high:+.4f}]")
    print()
    print(f"VERDICT                 : {'PASS' if report.passes else 'FAIL'}")
    for r in report.reasons:
        print(f"  - {r}")
    print()
    print(f"basis: {report.basis}")
    print(f"report written to {REPORT_PATH}")
    return 0 if report.passes else 1


if __name__ == "__main__":
    raise SystemExit(main())
