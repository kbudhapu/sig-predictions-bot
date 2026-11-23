#!/usr/bin/env bash
# Daily favorite-longshot SHADOW log (log-only, no orders). Appends a timestamped scan so the
# SIG->sharp convergence can be tracked as forward evidence BEFORE the 2026-11-04 settlement.
# Installed via crontab on the droplet:  0 13 * * * /opt/sig-bot/tools/fl_shadow_cron.sh
set -euo pipefail
cd /opt/sig-bot
TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT=/opt/sig-bot/data/fl_shadow/fl_scan_${TS}.log
mkdir -p /opt/sig-bot/data/fl_shadow
PYTHONPATH=/opt/sig-bot .venv/bin/python tools/favorite_longshot_scan.py > "$OUT" 2>&1 || true
echo "[$(date -u)] wrote $OUT ($(wc -l < "$OUT") lines)" >> /opt/sig-bot/data/fl_shadow/cron.log
