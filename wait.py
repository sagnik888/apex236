import time, requests

for _ in range(60):
    try:
        r = requests.get("http://127.0.0.1:8080/api/signals")
        data = r.json()
        if not data.get("scanning") and data.get("total_signals", 0) > 0:
            print("Server is ready! Total signals:", data["total_signals"])
            for s in data["signals"]:
                if s["symbol"] == "TCS":
                    print(f"TCS: ltp={s['option_ltp']}, entry={s['option_entry']}, sym={s['option_symbol']}")
            break
        else:
            print("Still scanning or 0 signals...")
    except Exception as e:
        print("Error:", e)
    time.sleep(5)
