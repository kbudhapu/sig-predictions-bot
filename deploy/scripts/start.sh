#!/usr/bin/env bash
# Enable + start the full sig-bot unit set in dependency order (trader first, then non-capital
# helpers, then the timers). `enable --now` so they also come back after a reboot. Run as root.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }

SERVICES=(
  sig-bot.service            # TRADER (single capital process) — first
  sig-bot-feed.service
  sig-bot-signal.service
  sig-bot-supervisor.service
  sig-bot-health.service
)
TIMERS=(
  sig-bot-watchdog.timer
  sig-bot-reconcile.timer
  sig-bot-pre-event.timer
  sig-bot-post-settlement.timer
)

for u in "${SERVICES[@]}"; do echo "== enabling $u"; systemctl enable --now "$u"; done
for t in "${TIMERS[@]}";   do echo "== enabling $t"; systemctl enable --now "$t"; done

echo "started. Verify: deploy/scripts/status.sh"
