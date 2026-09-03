import sqlite3
import pandas as pd
conn = sqlite3.connect(r'c:\Users\sagnik\Desktop\apex v-3\Apex100-apex100-2\artifacts\api-server\python_scanner\apex_trading.db')
query = """
SELECT timeframe, exit_reason, COUNT(*) as cnt, AVG(pnl) as avg_pnl 
FROM trades 
WHERE status='CLOSED' AND exit_reason IN ('Hard SL1', 'T1 Booked (User Target)', 'Momentum Exit')
GROUP BY timeframe, exit_reason
"""
print(pd.read_sql_query(query, conn).to_string(index=False))
conn.close()
