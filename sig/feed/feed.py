"""Realtime feed — WebSocket ingester with versioned-revision gap detection + reconnect-resync.

Core invariant (SIG_FEED_SPEC §1, §3): process updates IN REVISION ORDER. If an incoming update's
`previousRevision` != the last revision we processed, data was MISSED → we are STALE → refetch the
authoritative REST snapshot and resync. There is NO replay buffer, so a refetch is the ONLY recovery; a
reconnect ALWAYS refetches, never resumes mid-stream.

Ported mbappe discipline (read-only reference, Trading/):
  • Exponential reconnect backoff (control/heartbeat_emitter `_with_timeout_and_backoff`: delay =
    base·factor^(n−1), capped; a dropped connection must neither stall the loop nor be mistaken for death).
  • Reconcile-after-gap (execution/idempotent_execution.reconcile_boot: a gap means believed state may have
    drifted → SIGNAL reconciliation; NEVER silently adopt stream state across a gap — the REST snapshot is
    the truth we resync to). The feed fires `on_resync` so the OMS reconciles after every gap/reconnect.
  • Staleness-is-stale (infra/state: unknown/old age == stale). The feed SETS updated_at on every write; when
    it dies it stops writing, so state ages to stale and the risk gate's stale-data halt fires.

Dependency-injected so the WebSocket + REST are fully mockable (no live connection in tests):
  • `ws_factory(token)`  → an async context manager yielding an async-iterator of update dicts.
  • `token_provider()`   → (jwt, ttl_seconds).            default: POST /realtime/token via the read client.
  • `snapshot_fetcher()` → (price_rows, revision).        default: GET /exchanges/prices via the read client.
  • `on_resync(reason)`  → called after every (re)connect/gap so the OMS reconciles.
The real Supabase Realtime wire adapter is the one net-new integration seam (mbappe polls, never streams);
inject it as `ws_factory`. Everything else — the revision/resync/staleness/backoff/token logic — is here.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Awaitable, Callable


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@dataclass
class FeedConfig:
    token_ttl_s: float = 10800.0
    token_refresh_lead_s: float = 300.0
    reconnect_backoff_base_s: float = 1.0
    reconnect_backoff_factor: float = 2.0
    reconnect_backoff_cap_s: float = 60.0
    reconnect_reset_after_s: float = 120.0
    price_stale_after_s: float = 15.0
    snapshot_max_ids_per_call: int = 50

    @classmethod
    def from_config(cls, config: dict | None = None, config_path: str | None = None) -> "FeedConfig":
        if config is None:
            import os
            import yaml

            if config_path is None:
                here = os.path.dirname(os.path.abspath(__file__))
                root = os.path.dirname(os.path.dirname(here))  # sig/feed/ → repo root
                config_path = os.path.join(root, "config", "sig.yaml")
            with open(config_path, encoding="utf-8") as fh:
                config = yaml.safe_load(fh) or {}
        feed = (config or {}).get("feed", {})
        known = cls().__dict__
        return cls(**{k: feed[k] for k in known if k in feed})


@dataclass
class FeedHealth:
    """What the orchestrator / risk gate inspect. `stale` is the feed self-reporting it cannot be
    trusted (gap / disconnect / pre-first-snapshot); the per-exchange price age in state is the
    cross-process marker the risk gate keys off."""

    connected: bool = False
    stale: bool = True                      # starts STALE — nothing trusted until the first snapshot lands
    last_processed_revision: int | None = None
    last_snapshot_at: str | None = None
    token_expires_at: str | None = None
    reconnects: int = 0
    gaps: int = 0
    snapshots: int = 0
    updates_applied: int = 0
    reconcile_needed: bool = False          # set after any gap/reconnect; the OMS consumes + clears it

    def consume_reconcile_needed(self) -> bool:
        """The OMS calls this: True exactly once per gap/reconnect, then cleared."""
        was = self.reconcile_needed
        self.reconcile_needed = False
        return was


@dataclass
class FeedUpdate:
    """Parsed realtime update. `prices` are rows in the infra/state upsert_prices shape."""

    revision: int
    previous_revision: int | None
    prices: list[dict] = field(default_factory=list)
    fills: list[dict] = field(default_factory=list)

    @classmethod
    def parse(cls, raw: dict) -> "FeedUpdate":
        rev = raw.get("revision", raw.get("rev"))
        prev = raw.get("previousRevision", raw.get("previous_revision", raw.get("prevRevision")))
        return cls(
            revision=int(rev),
            previous_revision=(None if prev is None else int(prev)),
            prices=list(raw.get("prices", raw.get("data", [])) or []),
            fills=list(raw.get("fills", []) or []),
        )


class FeedError(RuntimeError):
    pass


# default-dependency type aliases
TokenProvider = Callable[[], Awaitable[tuple[str, float]]]
SnapshotFetcher = Callable[[], Awaitable[tuple[list[dict], int]]]
WsFactory = Callable[[str], Any]           # (token) -> async context manager → async-iter of raw dicts
ResyncHook = Callable[[str], Awaitable[None] | None]


class RealtimeFeed:
    def __init__(
        self,
        store: Any,
        *,
        config: FeedConfig | None = None,
        ws_factory: WsFactory,
        token_provider: TokenProvider | None = None,
        snapshot_fetcher: SnapshotFetcher | None = None,
        on_resync: ResyncHook | None = None,
        client: Any | None = None,
        exchange_ids: list[str] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now_fn: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.store = store
        self.cfg = config or FeedConfig()
        self._ws_factory = ws_factory
        self._client = client
        self._exchange_ids = exchange_ids or []
        self._token_provider = token_provider or self._default_token_provider
        self._snapshot_fetcher = snapshot_fetcher or self._default_snapshot_fetcher
        self._on_resync = on_resync
        self._sleep = sleep
        self._monotonic = monotonic
        self._now = now_fn
        self.health = FeedHealth()
        self._token: str | None = None

    # ── public inspection ──
    def is_stale(self) -> bool:
        return self.health.stale

    # ── default dependencies: REUSE the repo read client's auth (do not rewrite) ──
    async def _default_token_provider(self) -> tuple[str, float]:
        if self._client is None:
            raise FeedError("no read client injected for the default token provider")
        data = await asyncio.to_thread(self._client.get, "/realtime/token")
        token = (data or {}).get("token") or (data or {}).get("accessToken") or (data or {}).get("jwt")
        if not token:
            raise FeedError("no token in /realtime/token response")
        ttl = float((data or {}).get("expiresIn") or self.cfg.token_ttl_s)
        return str(token), ttl

    async def _default_snapshot_fetcher(self) -> tuple[list[dict], int]:
        if self._client is None:
            raise FeedError("no read client injected for the default snapshot fetcher")
        rows: list[dict] = []
        n = max(1, int(self.cfg.snapshot_max_ids_per_call))
        ids = self._exchange_ids
        # rate-limit-aware: batch ids, ≤ snapshot_max_ids_per_call per REST call (§3 non-negotiable)
        for i in range(0, max(1, len(ids)), n):
            batch = ids[i : i + n]
            resp = await asyncio.to_thread(self._client.exchange_prices, batch)
            rows.extend((resp or {}).get("data", []) or [])
        revision = 0
        if rows:
            seqs = [int(r.get("sequence", 0) or 0) for r in rows]
            revision = max(seqs) if seqs else 0
        return rows, revision

    # ── snapshot + resync (the ONLY recovery path — no replay buffer) ──
    async def _apply_snapshot_soft(self, *, context: str, retries: int = 3) -> int | None:
        """Apply the authoritative REST snapshot, but NEVER let a venue flap end the session.

        THE TEARDOWN BUG this fixes. _apply_snapshot runs between the socket joins and the update
        loop. The venue intermittently 500s on /exchanges/prices, the exception propagated out of
        _run_session, and run() treated it as a disconnect — throwing away a session that had just
        spent ~67s joining all 237 market channels. Observed 2026-10-02: two consecutive sessions
        died this way before a third got through (07:37:41 / 07:38:25 / 07:39:23), which looked
        exactly like a ~45s teardown timer.

        Proceeding WITHOUT a snapshot is safe:
          • continuity holds — the adapter SYNTHESISES revisions from feed health's current
            last_processed_revision (_primed_rev), so it primes from whatever value we have;
          • each market_batch carries the FULL book for that market, so WS re-establishes truth
            per market as updates arrive;
          • markets that never broadcast simply age out and the trader skips them as stale, and the
            REST safety net refreshes them.
        What we must NOT do is pretend we are fresh: on failure the feed is marked stale and
        reconcile_needed, so the risk gates stay conservative.
        """
        last: Exception | None = None
        for attempt in range(1, max(1, int(retries)) + 1):
            try:
                return await self._apply_snapshot()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — venue 5xx / transport blip
                last = exc
                await self._audit("feed_snapshot_retry", "warning",
                                  {"context": context, "attempt": attempt,
                                   "error": type(exc).__name__, "msg": str(exc)[:160]})
                if attempt < retries:
                    await asyncio.sleep(min(2.0 * attempt, 5.0))
        self.health.stale = True
        self.health.reconcile_needed = True
        await self._audit("feed_snapshot_failed_soft", "error",
                          {"context": context, "error": type(last).__name__ if last else "unknown",
                           "note": "keeping the session: the subscribe is far more expensive than "
                                   "the snapshot; feed marked STALE until updates arrive"})
        return None

    async def _apply_snapshot(self) -> int:
        rows, revision = await self._snapshot_fetcher()
        stamped = self._stamp(rows)
        await self.store.upsert_prices(stamped)
        self.health.last_processed_revision = revision
        self.health.last_snapshot_at = _iso(self._now())
        self.health.snapshots += 1
        self.health.stale = False  # fresh authoritative truth just landed
        await self._audit("feed_snapshot", "info", {"revision": revision, "rows": len(stamped)})
        return revision

    async def _resync(self, reason: str) -> int:
        # mark STALE first (gap/disconnect means we cannot trust the stream), THEN refetch + resync.
        self.health.stale = True
        self.health.reconcile_needed = True  # ported reconcile-after-gap: believed state may have drifted
        await self._audit("feed_resync_begin", "warning", {"reason": reason})
        revision = await self._apply_snapshot_soft(context=f"resync:{reason}")
        await self._signal_reconcile(reason)
        await self._audit("feed_resync_done", "info", {"reason": reason, "revision": revision,
                                                       "snapshot_ok": revision is not None})
        return revision if revision is not None else self.health.last_processed_revision

    def _stamp(self, rows: list[dict]) -> list[dict]:
        """Set the per-exchange staleness marker (updated_at) on every row. Unknown age == stale in
        infra/state, so a fresh updated_at is what keeps a price trusted; a dead feed stops stamping."""
        now = _iso(self._now())
        out = []
        for r in rows or []:
            r2 = dict(r)
            r2.setdefault("updated_at", now)
            out.append(r2)
        return out

    # ── revision-continuity: the safety heart ──
    def _is_continuous(self, update: FeedUpdate) -> bool:
        return update.previous_revision == self.health.last_processed_revision

    async def handle_update(self, raw: dict) -> str:
        """Process ONE update. Returns 'applied' | 'gap_resynced'. NEVER applies an out-of-order update
        as if continuous — a gap forces a mark-stale + authoritative refetch + resync."""
        update = raw if isinstance(raw, FeedUpdate) else FeedUpdate.parse(raw)
        if self._is_continuous(update):
            await self.store.upsert_prices(self._stamp(update.prices))
            self.health.last_processed_revision = update.revision
            self.health.updates_applied += 1
            if update.fills:
                # OBSERVE fills (OMS remains the single writer of authoritative order state) — audit only
                await self._audit("feed_fill_observed", "info", {"n": len(update.fills), "rev": update.revision})
            return "applied"
        # GAP: previousRevision != last_processed → missed data → stale → refetch → resync
        self.health.gaps += 1
        await self._audit(
            "feed_gap",
            "warning",
            {"incoming_prev": update.previous_revision, "last_processed": self.health.last_processed_revision,
             "incoming_rev": update.revision},
        )
        await self._resync("revision_gap")
        return "gap_resynced"

    # ── token ──
    async def _get_token(self) -> str:
        token, ttl = await self._token_provider()
        self._token = token
        self.health.token_expires_at = _iso(self._now() + timedelta(seconds=float(ttl)))
        return token

    def _token_expiring(self) -> bool:
        exp = self.health.token_expires_at
        if exp is None:
            return True
        try:
            expires = datetime.fromisoformat(exp)
        except ValueError:
            return True
        return self._now() + timedelta(seconds=self.cfg.token_refresh_lead_s) >= expires

    # ── one connected session: token → ws → snapshot → subscribe → consume updates ──
    async def _run_session(self) -> str:
        """Returns a reason string for why the session ended ('token_refresh' | 'disconnect' | 'stopped')."""
        token = await self._get_token()
        ctx = self._ws_factory(token)
        async with ctx as ws:
            self.health.connected = True
            # RECONNECT ALWAYS REFETCHES — the authoritative snapshot is the only resync (no replay
            # buffer). FAIL SOFT: a venue 500 here must not discard the ~67s channel subscribe.
            await self._apply_snapshot_soft(context="connect")
            # reconcile on every (re)connect: believed order/position state may have drifted (ported)
            self.health.reconcile_needed = True
            await self._signal_reconcile("connect")
            try:
                async for raw in ws:
                    if self._token_expiring():
                        return "token_refresh"   # planned reconnect — refresh the 3h JWT before expiry
                    await self.handle_update(raw)
            finally:
                self.health.connected = False
        return "disconnect"

    # ── the supervised run loop: reconnect w/ exponential backoff; dead feed → STALE ──
    async def run(self, *, stop_event: asyncio.Event | None = None, max_sessions: int | None = None) -> None:
        failures = 0
        sessions = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if max_sessions is not None and sessions >= max_sessions:
                break
            sessions += 1
            started = self._monotonic()
            reason = "disconnect"
            try:
                reason = await self._run_session()
            except asyncio.CancelledError:
                self.health.connected = False
                self.health.stale = True
                raise
            except Exception as exc:  # any ws/REST/token failure: a disconnect, never a crash of the loop
                reason = "error"
                await self._audit("feed_session_error", "error", {"error": type(exc).__name__, "msg": str(exc)[:160]})

            # the session ended → we are no longer streaming → mark STALE (a dead feed is never silently fresh)
            self.health.connected = False
            self.health.stale = True
            lasted = self._monotonic() - started

            if reason == "token_refresh":
                # planned: refresh + reconnect seamlessly, NO backoff penalty
                self.health.reconnects += 1
                await self._audit("feed_token_refresh", "info", {})
                failures = 0
                continue

            # unplanned disconnect/error → exponential backoff so repeated reconnects don't storm the
            # read budget; a connection that stayed up long enough resets the backoff to base.
            if lasted >= self.cfg.reconnect_reset_after_s:
                failures = 0
            failures += 1
            self.health.reconnects += 1
            delay = min(
                self.cfg.reconnect_backoff_cap_s,
                self.cfg.reconnect_backoff_base_s * (self.cfg.reconnect_backoff_factor ** (failures - 1)),
            )
            await self._audit("feed_reconnect_backoff", "warning", {"failures": failures, "delay_s": round(delay, 2)})
            await self._sleep(delay)

    # ── hooks / audit ──
    async def _signal_reconcile(self, reason: str) -> None:
        if self._on_resync is None:
            return
        res = self._on_resync(reason)
        if asyncio.iscoroutine(res):
            await res

    async def _audit(self, event_type: str, severity: str, payload: dict) -> None:
        fn = getattr(self.store, "append_audit", None)
        if fn is None:
            return
        try:
            await fn(event_type=event_type, severity=severity, component="feed", payload=payload)
        except Exception:
            pass  # observability must never break the feed
