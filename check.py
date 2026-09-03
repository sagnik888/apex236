import sqlite3, pandas as pd; conn = sqlite3.connect('artifacts/api-server/python_scanner/apex_trading.db'); print(pd.read_sql("SELECT * FROM trades WHERE symbol = 'NATIONALUM'", conn).to_string())
