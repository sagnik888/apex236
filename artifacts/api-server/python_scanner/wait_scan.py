import urllib.request
import json
import time

print("Waiting for scan to finish...")
while True:
    try:
        req = urllib.request.urlopen("http://localhost:8080/api/signals")
        data = json.loads(req.read())
        if not data.get("scanning", True):
            print("Scan finished!")
            for s in data['signals']:
                if s['symbol'] == 'RELIANCE':
                    print("RELIANCE:")
                    print("  Close:", s['close'])
                    print("  Day Chg Pct:", s['daily_move_pct'])
                    print("  Score:", s['score'])
                    break
            break
    except Exception as e:
        print("Error:", e)
    time.sleep(5)
