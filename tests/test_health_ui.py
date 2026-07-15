from fastapi.testclient import TestClient

from kline.app import create_app


def test_health_ui_is_browser_visible():
    with TestClient(create_app()) as client:
        response = client.get("/health-ui")
    assert response.status_code == 200
    assert "Datafeed Source Health" in response.text
    assert "fetch('/api/health')" in response.text
