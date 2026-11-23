"""Shared state layer — the single source of truth the two loops and the feed coordinate through.

One process, local SQLite in WAL mode, no network DB (integration spec §5). The fast arb loop, the
slow strategy loop, the realtime feed and the supervisor all read and write this one file:

    store = await StateStore().open()
    await store.upsert_prices(feed_rows)              # feed: primary writer of market state
    snap = await store.reread()                       # loop: cycle-top, disk is truth
    if await store.is_price_stale(ex, max_age_seconds=15): ...
    exposure = await store.read_exposure()            # caps bind on the COMBINED book
    res = await store.write_order(idempotency_key=k, ...)
    if not res.inserted: ...                          # dedup fired — already placed
    await store.open_basket(bid, ...)                 # atomic entry, remembered so it can be exited
    report = await store.mark_to_market()             # real unrealized_pnl; report.skipped = refusals
    for basket in await store.read_open_baskets(): ...  # legs recoverable as ONE group

Ported from mbappe (read-only reference): the WAL + busy_timeout pragma set and the rationale for it
(`src/persistence/db_queue.py::_connect_wal`), the off-event-loop write discipline (its threaded
`AsyncDBWriter`, re-expressed here with `asyncio.to_thread`), the append-only journal pattern
(`src/persistence/append_only.py`) and the env-aware data-root seam. The SIG schema is fresh, built
from the integration spec's §7 handoff contracts.
"""

from __future__ import annotations

from .schema import DDL, SCHEMA_VERSION, STATE_DB_PATH, connect, init_state_db
from .store import (
    Exposure,
    MarkReport,
    OrderWriteResult,
    StateSnapshot,
    StateStore,
    age_seconds,
    unrealized_pnl_for,
)

__all__ = [
    "StateStore",
    "StateSnapshot",
    "Exposure",
    "MarkReport",
    "OrderWriteResult",
    "age_seconds",
    "unrealized_pnl_for",
    "STATE_DB_PATH",
    "init_state_db",
    "connect",
    "DDL",
    "SCHEMA_VERSION",
]
