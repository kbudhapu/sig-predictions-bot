#!/usr/bin/env bash
# Thin wrapper the periodic timers invoke (reconcile / pre-event-check / post-settlement). Keeps the
# job→CLI mapping in deploy/ (ops-owned) rather than baking venv paths into every unit. The subcommand
# itself is the module contract (main_sig.py <job>); this script only locates the venv and logs.
set -euo pipefail

APP_DIR="${SIG_BOT_DIR:-/opt/sig-bot}"
PY="${APP_DIR}/.venv/bin/python"
JOB="${1:?usage: periodic_job.sh <reconcile|pre-event-check|post-settlement>}"

cd "$APP_DIR"
echo "sig-bot periodic job '${JOB}' starting $(date -u +%FT%TZ)"
# exec so systemd tracks the python process directly (correct exit status → oneshot result).
exec "$PY" "${APP_DIR}/main_sig.py" "$JOB"
