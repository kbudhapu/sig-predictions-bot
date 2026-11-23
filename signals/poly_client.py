"""Read-only ASYNC client for Polymarket's FREE public API (gamma + CLOB) — NO key, NO scraping, NO LLM.

Endpoints (confirmed against docs.polymarket.com, 2026-10):
  Gamma  https://gamma-api.polymarket.com
    GET /markets        params: closed, active, limit, offset, tag_id, slug, id
                        → market objects; outcomes / outcomePrices / clobTokenIds are JSON-encoded
                          array STRINGS (e.g. '["Yes","No"]', '["0.71","0.29"]').
    GET /markets/{id}   → one market object.
  CLOB   https://clob.polymarket.com
    GET /midpoint       params: token_id            → {"mid": "0.71"}  (order-book midpoint; freshest)
    GET /price          params: token_id, side      → {"price": "0.71"} (best bid for buy / ask for sell)

Public market data needs no credentials (docs: "public market data is available without credentials").
A REAL User-Agent is set (default UAs can be blocked). 429/5xx are retried with Retry-After + exponential
backoff. This module FETCHES numbers; all probability math is deterministic and lives in fair_value.py.
"""
from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass, field

import httpx

# ── logging: structlog if available, else a tiny stdlib shim with the same call shape (log.info(evt, **kw)) ──
try:  # pragma: no cover - trivial import glue
    import structlog

    log = structlog.get_logger("signals.poly_client")
except ModuleNotFoundError:  # pragma: no cover
    import logging

    _base = logging.getLogger("signals.poly_client")

    class _ShimLogger:
        def _emit(self, level: str, event: str, **kw) -> None:
            extra = " " + " ".join(f"{k}={v}" for k, v in kw.items()) if kw else ""
            getattr(_base, level)(event + extra)

        def debug(self, event, **kw): self._emit("debug", event, **kw)
        def info(self, event, **kw): self._emit("info", event, **kw)
        def warning(self, event, **kw): self._emit("warning", event, **kw)
        def error(self, event, **kw): self._emit("error", event, **kw)

    log = _ShimLogger()


GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
USER_AGENT = "sig-bot/0.1 research-signal (+https://github.com/kbudhapu/sig-bot; prediction-cup)"


class PolymarketError(RuntimeError):
    """A non-2xx or transport error from Polymarket. Carries status + path only (no secrets exist here)."""

    def __init__(self, status: int, path: str, detail: str = "") -> None:
        self.status = status
        self.path = path
        super().__init__(f"Polymarket {status} on {path}" + (f": {detail[:200]}" if detail else ""))


def _as_list(v) -> list:
    """Gamma encodes outcomes/outcomePrices/clobTokenIds as JSON array STRINGS; tolerate real lists too."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            return [v]
    return [v]


def _to_float(x, default: float | None = None) -> float | None:
    try:
        f = float(x)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


@dataclass
class Market:
    """A parsed Polymarket (gamma) market. Prices are last-trade/gamma prices in [0,1] per outcome."""

    id: str
    question: str
    slug: str
    outcomes: list[str]
    outcome_prices: list[float]
    clob_token_ids: list[str]
    closed: bool = False
    active: bool = True
    volume: float | None = None
    liquidity: float | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_gamma(cls, m: dict) -> "Market":
        prices = [p for p in (_to_float(x) for x in _as_list(m.get("outcomePrices"))) if p is not None]
        return cls(
            id=str(m.get("id", "")),
            question=str(m.get("question", "")),
            slug=str(m.get("slug", "")),
            outcomes=[str(o) for o in _as_list(m.get("outcomes"))],
            outcome_prices=prices,
            clob_token_ids=[str(t) for t in _as_list(m.get("clobTokenIds"))],
            closed=bool(m.get("closed", False)),
            active=bool(m.get("active", True)),
            volume=_to_float(m.get("volume")),
            liquidity=_to_float(m.get("liquidity")),
            raw=m,
        )

    def _index_of(self, outcome: str) -> int | None:
        target = outcome.strip().lower()
        for i, o in enumerate(self.outcomes):
            if o.strip().lower() == target:
                return i
        return None

    def token_for(self, outcome: str) -> str | None:
        i = self._index_of(outcome)
        return self.clob_token_ids[i] if i is not None and i < len(self.clob_token_ids) else None

    def gamma_price_for(self, outcome: str) -> float | None:
        i = self._index_of(outcome)
        return self.outcome_prices[i] if i is not None and i < len(self.outcome_prices) else None

    @property
    def yes_token_id(self) -> str | None:
        return self.token_for("Yes")

    @property
    def yes_price(self) -> float | None:
        return self.gamma_price_for("Yes")


class PolymarketClient:
    """Thin async read client. Use as an async context manager. No key needed for public data.

    `transport` lets tests inject an httpx.MockTransport so NO real HTTP happens offline.
    """

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        user_agent: str = USER_AGENT,
        gamma_base: str = GAMMA_BASE,
        clob_base: str = CLOB_BASE,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        backoff_cap: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._gamma = gamma_base.rstrip("/")
        self._clob = clob_base.rstrip("/")
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            transport=transport,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "PolymarketClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ── core GET with graceful 429/5xx backoff (honors Retry-After) ──
    async def _get_json(self, url: str, params: dict | None = None):
        attempt = 0
        while True:
            try:
                resp = await self._client.get(url, params=params)
            except httpx.HTTPError as e:
                # transport-level: retry a couple of times, then surface
                if attempt >= self._max_retries:
                    raise PolymarketError(0, url, f"transport:{type(e).__name__}") from e
                await asyncio.sleep(self._backoff(attempt))
                attempt += 1
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= self._max_retries:
                    raise PolymarketError(resp.status_code, url, "rate_limited_or_unavailable")
                delay = self._retry_after(resp) or self._backoff(attempt)
                log.warning("polymarket_retry", status=resp.status_code, attempt=attempt, delay=round(delay, 2))
                await asyncio.sleep(delay)
                attempt += 1
                continue

            if resp.status_code >= 400:
                raise PolymarketError(resp.status_code, url, resp.text)

            try:
                return resp.json()
            except ValueError as e:
                raise PolymarketError(resp.status_code, url, "non-json-body") from e

    def _backoff(self, attempt: int) -> float:
        return min(self._backoff_cap, self._backoff_base * (2 ** attempt))

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        ra = resp.headers.get("retry-after")
        if ra is None:
            return None
        try:
            return max(0.0, float(ra))
        except ValueError:
            return None  # HTTP-date form not parsed; fall back to exponential backoff

    # ── Gamma: markets ──
    async def get_markets(
        self,
        *,
        closed: bool | None = False,
        active: bool | None = True,
        limit: int = 100,
        offset: int = 0,
        tag_id: int | None = None,
        slug: str | None = None,
        ids: list[str] | None = None,
    ) -> list[Market]:
        params: dict = {"limit": limit, "offset": offset}
        if closed is not None:
            params["closed"] = str(closed).lower()
        if active is not None:
            params["active"] = str(active).lower()
        if tag_id is not None:
            params["tag_id"] = tag_id
        if slug is not None:
            params["slug"] = slug
        if ids:
            params["id"] = ids  # httpx serializes a list to repeated ?id=..&id=..
        data = await self._get_json(f"{self._gamma}/markets", params=params)
        rows = data if isinstance(data, list) else data.get("data", []) if isinstance(data, dict) else []
        return [Market.from_gamma(m) for m in rows]

    async def get_market(self, market_id: str) -> Market | None:
        data = await self._get_json(f"{self._gamma}/markets/{market_id}")
        if isinstance(data, list):
            return Market.from_gamma(data[0]) if data else None
        return Market.from_gamma(data) if data else None

    # ── CLOB: prices ──
    async def get_midpoint(self, token_id: str) -> float | None:
        d = await self._get_json(f"{self._clob}/midpoint", params={"token_id": token_id})
        return _to_float(d.get("mid") if isinstance(d, dict) else None)

    async def get_price(self, token_id: str, side: str = "buy") -> float | None:
        d = await self._get_json(f"{self._clob}/price", params={"token_id": token_id, "side": side})
        return _to_float(d.get("price") if isinstance(d, dict) else None)

    async def get_market_probability(
        self, market: Market, outcome: str = "Yes", *, prefer_clob: bool = True
    ) -> float | None:
        """The clean 'Polymarket probability for this race's outcome' → a SourceEstimate.p input.

        Prefers the freshest CLOB order-book midpoint; falls back to the gamma last-trade price.
        """
        token = market.token_for(outcome)
        if prefer_clob and token:
            try:
                mid = await self.get_midpoint(token)
                if mid is not None:
                    return mid
            except PolymarketError as e:
                log.warning("midpoint_failed_fallback_gamma", token=token, status=e.status)
        return market.gamma_price_for(outcome)
