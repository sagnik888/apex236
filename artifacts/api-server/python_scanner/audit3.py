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

run_query("Volume by timeframe and week", """
SELECT strftime('%Y-%W', entry_time) as week, timeframe, COUNT(*) as total
FROM trades WHERE status='CLOSED'
GROUP BY week, timeframe ORDER BY week, timeframe
""")

run_query("EOD square off by timeframe", """
SELECT timeframe, COUNT(*) as eod_exits 
FROM trades WHERE status='CLOSED' AND exit_reason = 'Intraday Square-Off (EOD)'
GROUP BY timeframe
""")

conn.close()
