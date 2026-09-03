import sqlite3
import os
import pandas as pd

DB_PATH = r"c:\Users\sagnik\Desktop\apex v-3\Apex100-apex100-2\artifacts\api-server\python_scanner\apex_trading.db"

conn = sqlite3.connect(DB_PATH)

def run_query(title, query):
    print(f"=== {title} ===")
    try:
        df = pd.read_sql_query(query, conn)
        print(df.to_string(index=False))
    except Exception as e:
        print(f"Error: {e}")
    print("\n")

print("Checking db size...")
cursor = conn.cursor()
cursor.execute("SELECT COUNT(*) FROM trades")
print(f"Total rows in trades: {cursor.fetchone()[0]}")
cursor.execute("SELECT COUNT(*) FROM trades WHERE status='CLOSED'")
print(f"Total CLOSED trades: {cursor.fetchone()[0]}")

print("Checking NULLs...")
cursor.execute("SELECT COUNT(*) FROM trades WHERE status='CLOSED' AND pnl IS NULL")
print(f"CLOSED trades with NULL pnl: {cursor.fetchone()[0]}")

run_query("Query 1: Overall win rate", """
SELECT 
  COUNT(*) as total, 
  SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
  SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
  AVG(pnl) as avg_pnl
FROM trades WHERE status='CLOSED' AND pnl IS NOT NULL
""")

run_query("Query 2: Win rate by timeframe", """
SELECT timeframe, COUNT(*) as total,
  SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
  AVG(pnl) as avg_pnl,
  CAST(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS FLOAT) / COUNT(*) as win_rate
FROM trades WHERE status='CLOSED' AND pnl IS NOT NULL
GROUP BY timeframe ORDER BY win_rate DESC
""")

run_query("Query 3: Win rate by direction (BUY vs SELL)", """
SELECT direction, COUNT(*) as total,
  SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
  AVG(pnl) as avg_pnl,
  CAST(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS FLOAT) / COUNT(*) as win_rate
FROM trades WHERE status='CLOSED' AND pnl IS NOT NULL
GROUP BY direction
""")

run_query("Query 4: Win rate by exit_reason", """
SELECT exit_reason, COUNT(*) as total,
  SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
  AVG(pnl) as avg_pnl
FROM trades WHERE status='CLOSED' AND pnl IS NOT NULL
GROUP BY exit_reason ORDER BY total DESC
""")

# Average bars held? We might need to estimate or use signal_time / entry_time vs exit_time.
run_query("Query 5: Average hold time (hours) for winners vs losers", """
SELECT 
  CASE WHEN pnl > 0 THEN 'WIN' ELSE 'LOSS' END as outcome,
  COUNT(*) as count,
  AVG( (julianday(exit_time) - julianday(entry_time)) * 24 ) as avg_hold_hours
FROM trades WHERE status='CLOSED' AND exit_time IS NOT NULL AND entry_time IS NOT NULL AND pnl IS NOT NULL
GROUP BY CASE WHEN pnl > 0 THEN 'WIN' ELSE 'LOSS' END
""")

run_query("Query 6: Win rate by hour of day (entry time)", """
SELECT strftime('%H', entry_time) as entry_hour, COUNT(*) as total,
  SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
  CAST(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS FLOAT) / COUNT(*) as win_rate
FROM trades WHERE status='CLOSED' AND pnl IS NOT NULL
GROUP BY entry_hour ORDER BY entry_hour
""")

run_query("Query 8: Average winner size vs average loser size (profit factor)", """
SELECT 
  AVG(CASE WHEN pnl > 0 THEN pnl END) as avg_win,
  AVG(CASE WHEN pnl < 0 THEN pnl END) as avg_loss,
  ABS(SUM(CASE WHEN pnl > 0 THEN pnl END) / SUM(CASE WHEN pnl < 0 THEN pnl END)) as profit_factor
FROM trades WHERE status='CLOSED' AND pnl IS NOT NULL
""")

run_query("Query 10: Win rate trend over time (weekly)", """
SELECT strftime('%Y-%W', entry_time) as week, COUNT(*) as total,
  SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
  CAST(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS FLOAT) / COUNT(*) as win_rate
FROM trades WHERE status='CLOSED' AND pnl IS NOT NULL
GROUP BY week ORDER BY week
""")

run_query("Query 11: Win rate by symbol (worst 10)", """
SELECT symbol, COUNT(*) as total,
  SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
  AVG(pnl) as avg_pnl,
  CAST(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS FLOAT) / COUNT(*) as win_rate
FROM trades WHERE status='CLOSED' AND pnl IS NOT NULL
GROUP BY symbol HAVING total > 5
ORDER BY win_rate ASC LIMIT 10
""")

run_query("Query 12: Distribution of PnL", """
SELECT 
  SUM(CASE WHEN pnl > 5 THEN 1 ELSE 0 END) as huge_wins,
  SUM(CASE WHEN pnl > 2 AND pnl <= 5 THEN 1 ELSE 0 END) as big_wins,
  SUM(CASE WHEN pnl > 0 AND pnl <= 2 THEN 1 ELSE 0 END) as small_wins,
  SUM(CASE WHEN pnl < 0 AND pnl >= -2 THEN 1 ELSE 0 END) as small_losses,
  SUM(CASE WHEN pnl < -2 AND pnl >= -5 THEN 1 ELSE 0 END) as big_losses,
  SUM(CASE WHEN pnl < -5 THEN 1 ELSE 0 END) as huge_losses
FROM trades WHERE status='CLOSED' AND pnl IS NOT NULL
""")

run_query("Query 14: How many trades hit TP1 but then reversed to SL?", """
SELECT COUNT(*) as count
FROM trades 
WHERE status='CLOSED' AND t1_hit = 1 AND pnl < 0
""")

run_query("Zombie Check", """
SELECT status, exit_reason, COUNT(*) as cnt
FROM trades
GROUP BY status, exit_reason
ORDER BY cnt DESC
""")

conn.close()
