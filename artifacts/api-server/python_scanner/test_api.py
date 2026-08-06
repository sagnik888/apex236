from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_get_signals():
    response = client.get("/api/signals")
    assert response.status_code == 200
    data = response.json()
    assert "signals" in data
    # Ensure it's a list
    assert isinstance(data["signals"], list)
