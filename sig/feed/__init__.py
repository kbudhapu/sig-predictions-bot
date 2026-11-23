"""Realtime market-data feed (SIG_FEED_SPEC) — the WebSocket ingester that keeps infra/state fresh.

The feed is the single writer of market prices: it subscribes to SIG's Supabase Realtime stream, applies
versioned updates IN REVISION ORDER, and on a revision GAP (or any reconnect) refetches the authoritative
REST snapshot and resyncs — because the stream has no replay buffer. It sets the per-exchange staleness
markers on every write, so a dead feed ages state to STALE (never silently fresh), and signals OMS
reconciliation after any gap/reconnect (order state may have drifted while the feed was out).
"""
from __future__ import annotations

from .feed import RealtimeFeed, FeedConfig, FeedHealth, FeedUpdate

__all__ = ["RealtimeFeed", "FeedConfig", "FeedHealth", "FeedUpdate"]
