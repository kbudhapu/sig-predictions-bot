"""REST price-polling loop — the INTERIM price transport.

The integration spec assumes a Supabase realtime (websocket) feed keeps StateStore prices fresh.
That WS seam is NOT built yet. This module is the interim replacement: it pulls NBBO prices over the
SIG REST read endpoint (GET /exchanges/prices) on a fixed interval and upserts them into the
StateStore, so every loop's staleness check (StateStore.is_price_stale / stale_exchanges) sees fresh
timestamps without the websocket. Swap this out for the WS consumer once that seam exists; the
StateStore contract (upsert_prices) stays identical, so nothing downstream changes.

The SIG read client (sig.sig_client.SigClient) is SYNC (httpx.Client); each network call is pushed to
a worker thread via asyncio.to_thread so it never blocks the event loop.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog

log = structlog.get_logger()


def _now_iso() -> str:
    """ISO8601 UTC — matches infra/state/store.py's house convention."""
    return datetime.now(timezone.utc).isoformat()


async def poll_prices_once(
    *,
    store,
    client,
    exchange_ids: list[str],
    max_ids_per_call: int = 50,
    now_fn=None,
) -> int:
    """Fetch NBBO prices for `exchange_ids` in batches and upsert them into the StateStore.

    `client.exchange_prices(batch)` is the SYNC SigClient read; each batch is wrapped in
    asyncio.to_thread so the sync httpx call never blocks the event loop. Returns the number of rows
    written. Rows missing bestBid/bestAsk are skipped (an incomplete quote must not overwrite a good
    one or masquerade as a fresh price).
    """
    now_iso = (now_fn or _now_iso)()
    ids = [str(i) for i in (exchange_ids or [])]
    if not ids:
        return 0

    rows: list[dict] = []
    for start in range(0, len(ids), max_ids_per_call):
        batch = ids[start:start + max_ids_per_call]
        resp = await asyncio.to_thread(client.exchange_prices, batch)
        data = resp.get("data", []) if isinstance(resp, dict) else []
        for r in data or []:
            if not isinstance(r, dict):
                continue
            bid = r.get("bestBid", r.get("best_bid"))
            ask = r.get("bestAsk", r.get("best_ask"))
            if bid is None or ask is None:
                continue                     # defensive: skip incomplete quotes
            ex = r.get("exchangeId", r.get("exchange_id", r.get("id")))
            if ex is None:
                continue
            rows.append({
                "exchangeId": ex,
                "bestBid": bid,
                "bestAsk": ask,
                "spread": r.get("spread"),
                "sequence": r.get("sequence"),
                "updated_at": now_iso,
            })

    if not rows:
        return 0
    return await store.upsert_prices(rows)


async def run_rest_price_poller(
    *,
    store,
    client,
    exchange_ids: list[str],
    interval_s: float = 5.0,
    max_ids_per_call: int = 50,
    stop_event: "asyncio.Event | None" = None,
) -> None:
    """Loop poll_prices_once every `interval_s` seconds until `stop_event` is set.

    A transient API error (rate limit, transport blip, bad payload) must NEVER kill the poller — a
    dead poller silently freezes every price, which the staleness guards would then (correctly) treat
    as a dead feed and halt trading. So each iteration's exceptions are caught, logged, and the loop
    continues. asyncio.CancelledError is re-raised so the task shuts down cleanly on cancel.
    """
    log.info("rest_price_poller_start", n_ids=len(exchange_ids or []), interval_s=interval_s,
             note="interim REST transport (WS seam not built)")
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            written = await poll_prices_once(
                store=store, client=client, exchange_ids=exchange_ids,
                max_ids_per_call=max_ids_per_call,
            )
            log.debug("rest_price_poll_ok", written=written)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("rest_price_poll_error", error=str(exc), error_type=type(exc).__name__,
                        note="transient — poller continues")
        await asyncio.sleep(interval_s)
    log.info("rest_price_poller_stopped")
