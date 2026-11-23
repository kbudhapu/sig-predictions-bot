#!/usr/bin/env bash
# Stop the sig-bot unit set. Stops the TRADER LAST so the helpers/monitors are still up to observe a
# clean trader shutdown. Does not `disable` (so a reboot still brings them back); pass --disable to
# also disable (full teardown). The trader stops via SIGINT (KillSignal) → graceful drain; positions
# are HELD (never auto-flattened on stop — flatten is a separate governance action).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
DISABLE="${1:-}"

TIMERS=(sig-bot-post-settlement.timer sig-bot-pre-event.timer sig-bot-reconcile.timer sig-bot-watchdog.timer)
HELPERS=(sig-bot-health.service sig-bot-supervisor.service sig-bot-signal.service sig-bot-feed.service)
TRADER=sig-bot.service

for t in "${TIMERS[@]}";  do systemctl stop "$t" || true; done
for h in "${HELPERS[@]}"; do systemctl stop "$h" || true; done
echo "== stopping trader (graceful SIGINT; positions HELD)"
systemctl stop "$TRADER" || true

if [ "$DISABLE" = "--disable" ]; then
  for u in "${TIMERS[@]}" "${HELPERS[@]}" "$TRADER"; do systemctl disable "$u" || true; done
  echo "disabled (full teardown)."
fi
echo "stopped."
