"""SIG Predictions Cup HTTP client — READ half (Phase 2). Write half (POST/DELETE) + the 429/Retry-After
handler + outbound write-gate are added in Phase 3 (sig/sig_write.py). The SIG endpoints sit behind
Cloudflare, so a REAL User-Agent is set (a default urllib/httpx UA can be blocked). The bearer key is read
from the gitignored .env via sig.env and is NEVER logged (errors carry status + path only, never the key).
"""
from __future__ import annotations

import os

import httpx
import structlog

from sig.env import require, load_env

log = structlog.get_logger()

DEFAULT_BASE_URL = "https://www.thesuper.market/api/v1"
USER_AGENT = "sig-bot/0.1 (+https://localhost; prediction-cup)"


class SigAPIError(RuntimeError):
    """A non-2xx SIG response. Carries status + path only — never the key or auth header."""

    def __init__(self, status: int, path: str, body_excerpt: str = "") -> None:
        self.status = status
        self.path = path
        super().__init__(f"SIG API {status} on {path}" + (f": {body_excerpt[:200]}" if body_excerpt else ""))


class SigClient:
    """Thin read client. One key (read scope) for Phase 2; Phase 3 adds per-key write budgeting."""

    def __init__(self, *, key_env: str = "SIG_API_KEY_READ", base_url: str | None = None, timeout: float = 15.0) -> None:
        load_env()
        self._key = require(key_env)                         # raises (redacted) if missing; never stored in logs
        self._base = (base_url or os.getenv("SIG_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._client = httpx.Client(
            base_url=self._base, timeout=timeout,
            headers={"Authorization": f"Bearer {self._key}", "User-Agent": USER_AGENT,
                     "Accept": "application/json"})

    # ── core GET (redacted errors; the key never appears in a message or log) ──
    def get(self, path: str, params: dict | None = None):
        try:
            resp = self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise SigAPIError(0, path, f"transport:{type(e).__name__}") from e
        if resp.status_code == 429:
            raise SigAPIError(429, path, "rate_limited")      # Phase-3 write-gate/handler acts on this
        if resp.status_code >= 400:
            raise SigAPIError(resp.status_code, path, resp.text)
        ctype = resp.headers.get("content-type", "")
        return resp.json() if "json" in ctype else resp.text

    # ── raw request (write path): returns status+body+retry_after WITHOUT raising on HTTP status, so the
    #    429/409/502/503 policy in sig.write_gate can classify. Transport errors still raise. ──
    def raw(self, method: str, path: str, *, json_body: dict | None = None, params: dict | None = None):
        from sig.write_gate import HttpResult
        try:
            resp = self._client.request(method, path, json=json_body, params=params)
        except httpx.HTTPError as e:
            raise SigAPIError(0, path, f"transport:{type(e).__name__}") from e
        ra = resp.headers.get("retry-after")
        retry_after = None
        if ra is not None:
            try:
                retry_after = float(ra)
            except ValueError:
                retry_after = None
        return HttpResult(status=resp.status_code, body=resp.text, retry_after_s=retry_after)

    # ── write endpoints (bodies per the SIG order schema). NOT called in Phase 2; driven by SigBroker. ──
    def place_order_raw(self, body: dict):
        return self.raw("POST", "/orders", json_body=body)

    def place_multi_leg_raw(self, legs: list[dict]):
        return self.raw("POST", "/orders/multi-leg", json_body={"legs": legs})

    def cancel_order_raw(self, order_id: str):
        return self.raw("DELETE", f"/orders/{order_id}")

    def cancel_all_raw(self):
        return self.raw("POST", "/orders/cancel-all")

    def order_by_idempotency_key(self, key: str):
        """Query-before-submit lookup. Tries a by-key order endpoint; returns the order dict or None.
        (Exact path confirmed against the first live order; tolerant of 404.)"""
        try:
            return self.get("/orders", params={"idempotencyKey": key})
        except SigAPIError as e:
            if e.status in (404, 400):
                return None
            raise

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SigClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── read endpoints (per the SIG spec) ──
    def account(self):
        return self.get("/account")

    def markets(self):
        return self.get("/markets")

    def exchange_prices(self, exchange_ids):
        """GET /exchanges/prices?ids=<comma-separated> -> {data:[{exchangeId,bestBid,bestAsk,spread,...}], missingIds}."""
        ids = exchange_ids if isinstance(exchange_ids, str) else ",".join(str(i) for i in exchange_ids)
        return self.get("/exchanges/prices", params={"ids": ids})

    def markets_all(self, page_limit: int = 100):   # SIG caps /markets limit at 100
        """Walk the /markets pagination (cursor) and return every market dict."""
        out, cursor = [], None
        while True:
            params = {"limit": page_limit}
            if cursor:
                params["cursor"] = cursor
            page = self.get("/markets", params=params)
            out.extend(page.get("data", []))
            pg = page.get("pagination") or {}
            if not pg.get("hasMore") or not pg.get("nextCursor"):
                break
            cursor = pg["nextCursor"]
        return out

    def orderbook(self, exchange_id):
        return self.get(f"/exchanges/{exchange_id}/orderbook")

    def exchange_trades(self, exchange_id, *, limit: int = 50, since_iso: str | None = None):
        """GET /exchanges/{id}/trades -> {data:[{price,size,side(YES|NO),volume,createdAt}], pagination}.

        Newest-first, YES-normalized prices. `side` is the AGGRESSOR's outcome space (YES = bought YES;
        NO = bought NO == sold YES), so it is a taker-flow signal. One read against the 100/min budget.
        """
        params = {"limit": min(int(limit), 200)}
        if since_iso:
            params["from"] = since_iso
        return self.get(f"/exchanges/{exchange_id}/trades", params=params)

    def price(self, exchange_id):
        return self.get(f"/exchanges/{exchange_id}/price")

    def price_history(self, exchange_id, *, resolution: str = "1m", limit: int = 1000,
                      from_iso: str | None = None, to_iso: str | None = None):
        """GET /exchanges/{id}/price-history -> {data:[Candle], from, to, coverage}.

        Candle = {time, open, high, low, close, vwap, volume, tradeCount}, YES-normalized, SPARSE
        (buckets with no trades are omitted). resolution in {1m,5m,1h,1d,1w}. One read.
        """
        params = {"resolution": resolution, "limit": min(int(limit), 1000)}
        if from_iso:
            params["from"] = from_iso
        if to_iso:
            params["to"] = to_iso
        return self.get(f"/exchanges/{exchange_id}/price-history", params=params)

    def leaderboard(self, slug: str):
        return self.get(f"/tournaments/{slug}/leaderboard")

    def relationships(self):
        return self.get("/relationships")

    def relationship_constraints(self):
        return self.get("/relationships/constraints")

    def portfolio_positions(self, slug: str | None = None):
        """Open positions with FIFO lots + unrealized P&L.

        NOTE the response shape: {"positions": [...], "summary": {...}} -- NOT {"data": [...]} like
        most read endpoints. Callers must not assume `data`.

        `slug` reads /tournaments/{slug}/portfolio/positions for an EXPLICIT tournament scope.
        Without it the endpoint resolves to the key's default tournament (org-bound key) or global
        positions (unbound key), which is not necessarily the book our orders go to."""
        if slug:
            return self.get(f"/tournaments/{slug}/portfolio/positions")
        return self.get("/portfolio/positions")

    def portfolio_fills(self, slug: str | None = None, limit: int = 200, cursor: str | None = None):
        """Fills carry the REAL entry prices. NB they have no buy/sell direction field -- deriving a
        net position from fills requires joining orderId back to the order's `action`. Paginated
        (pagination.nextCursor / hasMore; `cursor` fetches the next, older page)."""
        params = {"limit": limit, **({"cursor": cursor} if cursor else {})}
        if slug:
            return self.get(f"/tournaments/{slug}/portfolio/fills", params=params)
        return self.get("/portfolio/fills", params=params)
