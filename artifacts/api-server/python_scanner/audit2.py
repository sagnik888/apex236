import sqlite3
import pandas as pd

conn = sqlite3.connect(r'c:\Users\sagnik\Desktop\apex v-3\Apex100-apex100-2\artifacts\api-server\python_scanner\apex_trading.db')

def run_query(title, query):
    print(f"=== {title} ===")
    try:
        df = pd.read_sql_query(query, conn)
        print(df.to_string(index=False))
    except Exception as e:
        print(f"Error: {e}")
    print("\n")

run_query("Missing pnl check", "SELECT status, exit_reason, COUNT(*) as cnt FROM trades WHERE pnl IS NULL GROUP BY status, exit_reason")

run_query("T1 hits", "SELECT t1_hit, COUNT(*) as cnt FROM trades GROUP BY t1_hit")

conn.close()
