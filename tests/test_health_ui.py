from fastapi.testclient import TestClient

from kline.app import create_app


def test_health_ui_is_browser_visible():
    with TestClient(create_app()) as client:
        response = client.get("/health-ui")
    assert response.status_code == 200
    assert "资产 × 时间级别健康矩阵" in response.text
    assert "fetch(`${API}?_=${Date.now()}`" in response.text
    assert "最近一次运行" in response.text
    assert "market-filter" in response.text
    assert "timeframe-filter" in response.text
    assert "data-group-toggle" in response.text
    assert "MAX_SNAPSHOT_MS = 900000" in response.text
    assert "AbortController" in response.text
    assert "cache:'no-store'" in response.text
    assert "授权阻塞" in response.text
    assert "阻塞 ${blocked}" in response.text
    assert "重试" not in response.text
