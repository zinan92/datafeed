from pathlib import Path
import plistlib


PLIST_PATH = (
    Path(__file__).parents[1] / "ops" / "com.wendy.datafeed.health-dashboard.plist.example"
)
RUNTIME = "/Users/wendy/datafeed-runtime-health-dashboard"


def test_combined_health_dashboard_launchd_isolated_from_screening_worker() -> None:
    with PLIST_PATH.open("rb") as handle:
        payload = plistlib.load(handle)

    assert payload["Label"] == "com.wendy.datafeed.health-dashboard"
    assert payload["WorkingDirectory"] == RUNTIME
    arguments = payload["ProgramArguments"]
    assert arguments[-6:] == ["--host", "127.0.0.1", "--port", "18172", "--log-level", "info"]
    environment = payload["EnvironmentVariables"]
    assert environment["PYTHONPATH"] == f"{RUNTIME}/src"
    assert environment["KLINE_RUNTIME_ROOT"] == RUNTIME
    assert environment["KLINE_BUILD_SHA"] == "__KLINE_BUILD_SHA__"
    assert environment["KLINE_DB_PATH"].endswith("datafeed-runtime-issue-71/data/kline.db")
    assert environment["KLINE_MARKET_DB_PATH"] == "/Users/wendy/park-data/market/kline.db"
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["StandardOutPath"].endswith("/health-dashboard.stdout.log")
    assert payload["StandardErrorPath"].endswith("/health-dashboard.stderr.log")
