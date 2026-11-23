"""PORTED/adapted from mbappe api/main.py — the FastAPI control/monitoring SIDECAR (exchange-independent).

Exposes /health (unauth liveness) and /status (bearer-token-guarded snapshot read from the SQLite
source-of-truth: heartbeat age, degradation mode, recent operator alerts). Lets a human (or the Vercel
dashboard) monitor the bot REMOTELY without touching the trading process. Token from CONTROL_API_TOKEN.
Run: uvicorn api.main:app --host 127.0.0.1 --port 8787  (bind localhost or firewall to your IP).

Dependency note: needs `fastapi` + `uvicorn` in the sig-bot venv (added to requirements). The rest of the
bot does not import this module, so the sidecar being absent never affects trading.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException

from infra.config import DB_PATH
from infra.persistence import db
from infra.persistence.governance_state_store import load_governance_state
from infra.control.alerts import heartbeat_age_seconds

app = FastAPI(title="sig-bot sidecar", docs_url=None, redoc_url=None)


def _verify_token(authorization: str | None) -> None:
    expected = os.getenv("CONTROL_API_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="control API token not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if authorization.split(" ", 1)[1].strip() != expected:
        raise HTTPException(status_code=403, detail="bad token")


@app.get("/health")
def health() -> dict[str, Any]:
    """Unauthenticated liveness: process is up + how stale the engine heartbeat is."""
    age = heartbeat_age_seconds(db_path=DB_PATH)
    return {"ok": True, "heartbeat_age_seconds": age}


@app.get("/status")
def status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Bearer-guarded snapshot read from SQLite (no trading-process coupling)."""
    _verify_token(authorization)
    gov = load_governance_state(DB_PATH)
    return {
        "heartbeat_age_seconds": heartbeat_age_seconds(db_path=DB_PATH),
        "degradation_mode": gov.degradation_mode if gov else "NORMAL",
        "flatten_latched": bool(gov.flatten_latched) if gov else False,
        "recent_alerts": db.recent_alerts(limit=20),
        "recent_events": db.get_system_events(limit=20),
    }
