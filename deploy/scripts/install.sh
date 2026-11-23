#!/usr/bin/env bash
# Install sig-bot systemd units + the journald retention drop-in, then daemon-reload. Idempotent;
# run as root (sudo) on the droplet AFTER the repo is at /opt/sig-bot and the venv + .env exist.
# Does NOT start anything — the runbook enables units deliberately (SIM before live).
set -euo pipefail

APP_DIR="${SIG_BOT_DIR:-/opt/sig-bot}"
DEPLOY_DIR="${APP_DIR}/deploy"
UNIT_DST="/etc/systemd/system"
JOURNALD_DST="/etc/systemd/journald.conf.d"

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
[ -d "$DEPLOY_DIR" ] || { echo "no deploy dir at $DEPLOY_DIR"; exit 1; }

echo "== installing units → $UNIT_DST"
install -m 0644 "$DEPLOY_DIR"/sig-bot*.service "$UNIT_DST"/
install -m 0644 "$DEPLOY_DIR"/sig-bot*.timer   "$UNIT_DST"/

echo "== installing journald retention cap"
mkdir -p "$JOURNALD_DST"
install -m 0644 "$DEPLOY_DIR/journald/99-sig-bot.conf" "$JOURNALD_DST"/99-sig-bot.conf
systemctl restart systemd-journald

echo "== making ops scripts executable"
chmod 0755 "$DEPLOY_DIR"/scripts/*.sh

echo "== daemon-reload"
systemctl daemon-reload

echo "done. Units installed (NOT started). Next: enable per the runbook (SIM, then live)."
echo "  systemctl list-unit-files 'sig-bot*'"
