import sqlite3
conn = sqlite3.connect('artifacts/api-server/python_scanner/apex_trading.db')
cursor = conn.cursor()
cursor.execute('UPDATE trades SET signal_time = \
2026-08-14
00:00:00.000000\ WHERE id = 2347')
conn.commit()
print('Done')
