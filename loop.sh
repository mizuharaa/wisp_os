#!/usr/bin/env bash
# Daily production review — cron entrypoint. Runs review.py (scan -> escalate).
#
# INSTALL (do NOT let anything edit the operator's crontab automatically):
#   Add this line with `crontab -e`, absolute path, 07:00 sharp:
#
#     0 7 * * * /abs/path/to/agentic_os/loop.sh >> /abs/path/to/agentic_os/state/review/cron.log 2>&1
#
#   Test first, no side effects, no API key needed:  bash loop.sh --dry-run
#
# Cron-safe by construction: absolute paths (cron has almost no env), a minimal
# PATH/env set below, an atomic run-lock so overlapping fires can't stack, a
# once-per-day guard so a manual + cron run on the same day don't double-file,
# and all output tee'd to a dated logfile. --dry-run bypasses both guards.
set -euo pipefail

# --- absolute anchoring (cron does not cd for you) ---------------------------
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# --- env: cron starts nearly empty; set what review.py needs -----------------
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
export PYTHONUNBUFFERED=1
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python
# ANTHROPIC_API_KEY: inherit if cron's env has it; else review.py falls back to
# ROOT/.env (see model_client._api_key). Load .env here too so cron sees it.
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi

DRY_RUN=""
[ "${1:-}" = "--dry-run" ] && DRY_RUN="--dry-run"

LOGDIR="$ROOT/state/review"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/loop-$(date +%F).log"
STAMP="$LOGDIR/.last-run"          # once-per-day guard (YYYY-MM-DD)
LOCK="$LOGDIR/.lock"               # atomic run-lock (mkdir is atomic everywhere)

log() { echo "[$(date +%FT%T)] loop.sh: $*" | tee -a "$LOG"; }

if [ -z "$DRY_RUN" ]; then
  # once-per-day: skip if today already ran (idempotent under retries/manual)
  if [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$(date +%F)" ]; then
    log "already ran today ($(cat "$STAMP")) — skipping."; exit 0
  fi
  # atomic lock: mkdir succeeds for exactly one racer
  if ! mkdir "$LOCK" 2>/dev/null; then
    log "another run holds the lock ($LOCK) — skipping."; exit 0
  fi
  trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT
fi

log "starting review${DRY_RUN:+ (dry-run)}"
# review.py never hard-exits on a single model refusal; if the whole process
# dies we still want a clean lock release (trap above) and a logged failure.
if "$PYTHON" "$ROOT/review.py" $DRY_RUN 2>&1 | tee -a "$LOG"; then
  [ -z "$DRY_RUN" ] && date +%F > "$STAMP"
  log "review complete."
else
  log "review.py exited non-zero — see log above."; exit 1
fi
