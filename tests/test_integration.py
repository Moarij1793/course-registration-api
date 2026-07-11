import httpx


def test_live_api_health():
    response = httpx.get(
        "http://127.0.0.1:8000/",
        timeout=10,
    )

    assert response.status_code == 200
    assert response.json() == {"status": "online"}


    