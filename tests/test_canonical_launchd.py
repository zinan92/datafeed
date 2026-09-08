from pathlib import Path
import plistlib


ROOT = Path(__file__).parents[1]
CANONICAL = "/Users/wendy/park-runtime/datafeed"
JOBS = (
    "com.wendy.datafeed",
    "com.wendy.datafeed.mvp-api",
    "com.wendy.datafeed.mvp-worker",
    "com.wendy.datafeed.watchlist-daily",
    "com.wendy.datafeed.health-dashboard",
)


def test_all_managed_launchd_jobs_use_one_canonical_checkout() -> None:
    payloads = []
    for job in JOBS:
        with (ROOT / "ops" / "launchd" / f"{job}.plist").open("rb") as handle:
            payloads.append(plistlib.load(handle))

    assert {payload["WorkingDirectory"] for payload in payloads} == {CANONICAL}
    for payload in payloads:
        env = payload["EnvironmentVariables"]
        assert env["PYTHONPATH"] == f"{CANONICAL}/src"
        assert env["KLINE_RUNTIME_ROOT"] == CANONICAL
        assert env["KLINE_BUILD_SHA"] == "__KLINE_BUILD_SHA__"


def test_release_script_has_worker_gate_and_reversible_install() -> None:
    script = (ROOT / "ops" / "release.sh").read_text()
    assert "mvp-worker restart is inside the 20-minute safety window" in script
    assert "launchctl bootout" in script and "launchctl bootstrap" in script
    assert "cp -p \"$rendered\" \"$BACKUP_DIR/$job.plist\"" in script


def test_worker_keeps_current_stock_seed_contract() -> None:
    with (ROOT / "ops" / "launchd" / "com.wendy.datafeed.mvp-worker.plist").open("rb") as handle:
        payload = plistlib.load(handle)
    arguments = payload["ProgramArguments"]
    assert arguments[2] == "ops.mvp_stock_seed"
    assert arguments[arguments.index("--lock") + 1] == "/Users/wendy/park-data/market/mvp-worker.lock"
    assert arguments[-1] == "--forever"
