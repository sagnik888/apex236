"""De-duplicate the trades table so the unique identity index can be created.

A trade is identified by (symbol, timeframe, entry_time, direction). Without a
unique index the scan loop persisted the same closure repeatedly: 256 duplicate
identity groups accumulated, 33 of them carrying contradictory win/loss signs,
and every SQL statistic counted all of them.

DRY RUN BY DEFAULT. Nothing is deleted unless you pass --apply.

    python dedupe_trades.py              # report only
    python dedupe_trades.py --backup     # report + write a timestamped copy
    python dedupe_trades.py --apply      # delete (refuses without a backup)

Survivor selection, in order of preference within each group:
  1. A row whose exit_reason came from a real TradeRecord (not a System:/LEGACY
     synthetic close) — that is the row whose outcome was actually observed.
  2. A row with a non-NULL pnl over one without.
  3. A CLOSED row over an ACTIVE one.
  4. The lowest id, so the choice is deterministic.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "python_scanner" / "apex_trading.db"

SYNTHETIC = ("System:", "LEGACY_SYMBOL_CLEANUP", "REPAINT_EXIT")


def survivor_rank(row: sqlite3.Row) -> tuple:
    reason = row["exit_reason"] or ""
    synthetic = any(reason.startswith(p) or p in reason for p in SYNTHETIC)
    return (
        1 if synthetic else 0,            # real exits first
        0 if row["pnl"] is not None else 1,
        0 if row["status"] == "CLOSED" else 1,
        row["id"],
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually delete duplicate rows")
    ap.add_argument("--backup", action="store_true", help="write a timestamped copy of the DB first")
    args = ap.parse_args()

    if not DB.exists():
        print(f"database not found: {DB}")
        return 1

    if args.backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = DB.with_name(f"{DB.stem}.backup-{stamp}{DB.suffix}")
        shutil.copy2(DB, dest)
        print(f"backup written: {dest}")

    mode = "" if args.apply else "?mode=ro"
    conn = sqlite3.connect(f"file:{DB}{mode}", uri=True)
    conn.row_factory = sqlite3.Row

    groups = conn.execute(
        "SELECT symbol, timeframe, entry_time, direction, COUNT(*) n "
        "FROM trades GROUP BY symbol, timeframe, entry_time, direction "
        "HAVING COUNT(*) > 1 ORDER BY n DESC"
    ).fetchall()

    if not groups:
        print("no duplicate identity groups; the unique index can be created on next start.")
        return 0

    doomed: list[int] = []
    contradictory = 0
    for g in groups:
        rows = conn.execute(
            "SELECT * FROM trades WHERE symbol=? AND timeframe=? AND entry_time=? AND direction=?",
            (g["symbol"], g["timeframe"], g["entry_time"], g["direction"]),
        ).fetchall()
        signs = {(r["pnl"] or 0) > 0 for r in rows if r["pnl"] is not None}
        if len(signs) > 1:
            contradictory += 1
        keep = sorted(rows, key=survivor_rank)[0]
        doomed.extend(r["id"] for r in rows if r["id"] != keep["id"])

    total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    print(f"duplicate identity groups : {len(groups)}")
    print(f"  of which contradictory  : {contradictory} (rows disagree on win/loss)")
    print(f"rows to delete            : {len(doomed)}")
    print(f"trades table              : {total} -> {total - len(doomed)}")

    print("\nlargest groups:")
    for g in groups[:8]:
        print(f"  {g['symbol']:<14} {g['timeframe']:<4} {g['entry_time']}  {g['direction']:<4} x{g['n']}")

    if not args.apply:
        print("\nDRY RUN - nothing deleted. Re-run with --backup --apply to proceed.")
        return 0

    if not args.backup:
        print("\nrefusing to delete without --backup. Re-run with: --backup --apply")
        return 2

    cur = conn.cursor()
    cur.executemany("DELETE FROM trades WHERE id = ?", [(i,) for i in doomed])
    conn.commit()
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_identity "
        "ON trades (symbol, timeframe, entry_time, direction)"
    )
    conn.commit()
    print(f"\ndeleted {len(doomed)} rows; unique index created.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
