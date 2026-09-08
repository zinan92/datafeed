#!/bin/zsh
set -euo pipefail

CANONICAL_ROOT="${CANONICAL_ROOT:-$HOME/park-runtime/datafeed}"
LAUNCH_AGENTS="${LAUNCH_AGENTS:-$HOME/Library/LaunchAgents}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/park-data/launchd-backup-20260908}"
RECEIPT_PATH="${RECEIPT_PATH:-$HOME/park-data/datafeed/release_receipt.json}"
WORKER_LOG="${WORKER_LOG:-$HOME/Library/Logs/datafeed/mvp-worker.stdout.log}"
SCRIPT_ROOT="${0:A:h}"
JOBS=(com.wendy.datafeed com.wendy.datafeed.mvp-api com.wendy.datafeed.mvp-worker com.wendy.datafeed.watchlist-daily com.wendy.datafeed.health-dashboard)

[[ $# -eq 1 ]] || { print -u2 "usage: $0 <sha>"; exit 2; }
[[ -d "$CANONICAL_ROOT/.git" ]] || { print -u2 "canonical checkout missing: $CANONICAL_ROOT"; exit 1; }
git -C "$CANONICAL_ROOT" fetch origin main
[[ -z "$(git -C "$CANONICAL_ROOT" status --porcelain)" ]] || { print -u2 "canonical checkout is dirty"; exit 1; }
SHA="$(git -C "$CANONICAL_ROOT" rev-parse --verify "$1^{commit}")"
git -C "$CANONICAL_ROOT" checkout --detach "$SHA"
[[ -z "$(git -C "$CANONICAL_ROOT" status --porcelain)" ]] || { print -u2 "checkout is dirty after release checkout"; exit 1; }

python3 - "$WORKER_LOG" <<'PY'
import re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
matches = re.findall(r'"observed_at": "([^"]+)', Path(sys.argv[1]).read_text())
if not matches:
    raise SystemExit("cannot prove mvp-worker idle: no observed_at")
now = datetime.now(timezone.utc)
candidate = now.replace(minute=54, second=0, microsecond=0)
while candidate <= now:
    candidate += timedelta(hours=1)
while candidate.hour % 4 != 0:
    candidate += timedelta(hours=1)
if (candidate - now).total_seconds() < 1200:
    raise SystemExit("mvp-worker restart is inside the 20-minute safety window")
print(f"mvp-worker idle gate: last_observed={matches[-1]} next_cycle={candidate.isoformat()}")
PY

mkdir -p "$BACKUP_DIR" "${RECEIPT_PATH:h}"
for job in $JOBS; do
  source="$SCRIPT_ROOT/launchd/$job.plist"
  rendered="$LAUNCH_AGENTS/$job.plist"
  cp -p "$rendered" "$BACKUP_DIR/$job.plist"
  sed "s/__KLINE_BUILD_SHA__/$SHA/g" "$source" > "$rendered"
  plutil -lint "$rendered"
done

for job in $JOBS; do
  domain="gui/$(id -u)/$job"
  launchctl bootout "$domain"
  for attempt in 1 2 3; do
    if launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENTS/$job.plist"; then
      break
    fi
    [[ $attempt -lt 3 ]] || exit 1
    sleep 1
  done
done

python3 - "$RECEIPT_PATH" "$CANONICAL_ROOT" "$SHA" "${JOBS[@]}" <<'PY'
import json, sys
from datetime import datetime, timezone
from pathlib import Path
path, checkout, sha, *jobs = sys.argv[1:]
payload = {
    "schema_version": "datafeed-release-receipt-v1",
    "sha": sha,
    "released_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    "canonical_checkout": str(Path(checkout).expanduser()),
    "restarted_jobs": jobs,
}
Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
PY
