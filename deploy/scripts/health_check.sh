#!/usr/bin/env bash
# Liveness probe — polls the unauth /health endpoint of the sidecar (api/main.py). Exit 0 = healthy,
# 1 = unhealthy/unreachable. This is what an operator, an uptime monitor, or a reverse-proxy health
# check calls. (The out-of-process DEAD-MAN that restarts/pages is sig-bot-watchdog.* +
# sig-bot-supervisor, which read the SQLite heartbeat directly and do not depend on this HTTP path.)
#
# /health reports the backend checks adapted from mbappe: SIG API reachable, state.db writable, feed
# freshness, last-cycle-age. We treat the HTTP 200 + a truthy top-level status as healthy; if the
# payload carries an explicit {"ok": false} / {"status": "..."} we honour it.
set -uo pipefail

URL="${SIG_HEALTH_URL:-http://127.0.0.1:8787/health}"
TIMEOUT="${SIG_HEALTH_TIMEOUT:-5}"

body="$(curl -fsS --max-time "$TIMEOUT" "$URL" 2>/dev/null)"
rc=$?
if [ $rc -ne 0 ]; then
  echo "UNHEALTHY: /health unreachable ($URL), curl rc=$rc"
  exit 1
fi

# Fail closed on an explicit negative status; otherwise a 200 with a body is healthy.
if printf '%s' "$body" | grep -qiE '"(ok|healthy)"[[:space:]]*:[[:space:]]*false|"status"[[:space:]]*:[[:space:]]*"(unhealthy|degraded|stale|down)"'; then
  echo "UNHEALTHY: $body"
  exit 1
fi

echo "HEALTHY: $body"
exit 0
