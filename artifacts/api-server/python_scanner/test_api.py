import urllib.request
import json
req = urllib.request.urlopen("http://localhost:8080/api/signals")
data = json.loads(req.read())
for s in data['signals']:
    if s['symbol'] == 'RELIANCE':
        print(s['symbol'], "close:", s['close'], "daily_move_pct:", s['daily_move_pct'])
        break
