from fastapi.testclient import TestClient

from app.main import app, store

client = TestClient(app)

def setup_function() -> None:
    store.chunks.clear()

def test_health_endpoint() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

def test_query_returns_relevant_source() -> None:
    store.add("Before restarting a machine check the emergency stop and record the fault code.", "safety.md")
    response = client.post("/query", json={"question": "What should I check before restarting a machine?", "top_k": 3})
    assert response.status_code == 200
    body = response.json()
    assert body["sources"]
    assert body["sources"][0]["source"] == "safety.md"
    assert "emergency stop" in body["answer"]

def test_query_validates_question_length() -> None:
    response = client.post("/query", json={"question": "hi"})
    assert response.status_code == 422

def test_upload_plain_text_document() -> None:
    response = client.post(
        "/documents",
        files={"file": ("custom.txt", b"Reset the filter after replacing it.", "text/plain")},
    )
    assert response.status_code == 200
    assert response.json()["source"] == "custom.txt"
    assert response.json()["chunks"] >= 1

def test_upload_rejects_unsupported_extension() -> None:
    response = client.post(
        "/documents",
        files={"file": ("script.exe", b"not a document", "application/octet-stream")},
    )
    assert response.status_code == 415

def test_upload_rejects_empty_document() -> None:
    response = client.post(
        "/documents",
        files={"file": ("empty.txt", b"   ", "text/plain")},
    )
    assert response.status_code == 400
