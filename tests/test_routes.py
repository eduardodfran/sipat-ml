from fastapi.testclient import TestClient
from processing.main import app

client = TestClient(app)

r = client.post(
    "/upload/init",
    json={"video_filename": "t.mp4", "gps_filename": "t.json"},
    headers={"Authorization": "Bearer x"},
)
print(f"Status: {r.status_code}")
print(f"Body: {r.text[:500]}")
