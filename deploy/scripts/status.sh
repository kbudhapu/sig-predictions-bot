#!/usr/bin/env bash
# One-glance health: unit states, next timer fires, the /health probe, and recent trader log tail.
set -uo pipefail

echo "== units =="
systemctl --no-pager --no-legend list-units 'sig-bot*' 2>/dev/null || true
echo
echo "== timers (next fire) =="
systemctl --no-pager --no-legend list-timers 'sig-bot*' 2>/dev/null || true
echo
echo "== /health =="
"$(dirname "$0")/health_check.sh" || true
echo
echo "== trader log (last 20) =="
journalctl -u sig-bot.service -n 20 --no-pager 2>/dev/null || true
