from fastapi.testclient import TestClient

from kline.app import create_app


def test_health_ui_is_browser_visible():
    with TestClient(create_app()) as client:
        response = client.get("/health-ui")
    assert response.status_code == 200
    assert "资产 × 时间级别健康矩阵" in response.text
    assert "fetch(`${API}?_=${Date.now()}`" in response.text
    assert "最近一次运行" in response.text
    assert "重试" not in response.text
