from pathlib import Path
import plistlib


PLIST_PATH = (
    Path(__file__).parents[1] / "ops" / "com.wendy.datafeed.watchlist-daily.plist.example"
)
RUNTIME = "/Users/wendy/datafeed-runtime-watchlist"
DATABASE = "/Users/wendy/park-data/market/kline.db"
LOCK = "/Users/wendy/park-data/market/watchlist-worker.lock"


def test_watchlist_launchd_contract_is_daily_isolated_and_fail_closed() -> None:
    with PLIST_PATH.open("rb") as handle:
        payload = plistlib.load(handle)

    assert payload["Label"] == "com.wendy.datafeed.watchlist-daily"
    assert payload["WorkingDirectory"] == RUNTIME
    assert payload["EnvironmentVariables"]["PYTHONPATH"] == f"{RUNTIME}/src"
    assert payload["EnvironmentVariables"]["KLINE_RUNTIME_ROOT"] == RUNTIME
    assert payload["EnvironmentVariables"]["KLINE_BUILD_SHA"] == "__KLINE_BUILD_SHA__"
    arguments = payload["ProgramArguments"]
    assert arguments[:3] == ["/usr/local/bin/python3", "-m", "ops.watchlist_seed"]
    assert arguments[arguments.index("--manifest") + 1] == (
        f"{RUNTIME}/configs/watchlist_registry_manifest.json"
    )
    assert arguments[arguments.index("--db") + 1] == DATABASE
    assert arguments[arguments.index("--lock") + 1] == LOCK
    assert arguments[arguments.index("--request-interval") + 1] == "2"
    assert arguments[arguments.index("--receipt") + 1] == (
        "/Users/wendy/park-data/market/watchlist-latest.json"
    )
    assert payload["StandardOutPath"].endswith("/watchlist-daily.stdout.log")
    assert payload["StandardErrorPath"].endswith("/watchlist-daily.stderr.log")
    assert payload["StartCalendarInterval"] == [
        {"Weekday": weekday, "Hour": 8, "Minute": 15} for weekday in range(1, 6)
    ]
    for forbidden in ("RunAtLoad", "KeepAlive", "StartInterval"):
        assert forbidden not in payload
