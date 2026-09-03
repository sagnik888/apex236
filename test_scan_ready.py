import time, requests

for _ in range(30):
    try:
        r = requests.get("http://127.0.0.1:8080/api/signals")
        data = r.json()
        if not data.get("scanning"):
            print("Server is ready!")
            print(data.get("signals")[:2])
            break
        else:
            print("Scanning...")
    except Exception as e:
        print("Error:", e)
    time.sleep(10)
