"""Minimal async READ client for Kalshi's public elections API — the SECOND real-money sharp source for
the agreement gate (signals/sharp_benchmark). Market data (GET) needs no auth. Prices are normalized to a
[0,1] probability. Kalshi now quotes in DOLLAR strings (`yes_bid_dollars: "0.3400"`, `volume_fp`); the old
integer-cent fields (`yes_bid: 34`) are gone, which is why every market read as "no live price" on 2026-10-02.
parse_quote reads the dollar fields first and falls back to the cent fields.

Kalshi taxonomy: series -> events -> markets. A market's YES contract is a single outcome ("yes_sub_title"
names it, e.g. 'Democratic'). Mid = (yes_bid + yes_ask)/2; falls back to last_price when a side is empty.
"""
from __future__ import annotations

import asyncio

import httpx

try:  # pragma: no cover
    import structlog
    log = structlog.get_logger("kalshi_client")
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("kalshi_client")

DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiError(RuntimeError):
    def __init__(self, status: int, path: str, detail: str = "") -> None:
        super().__init__(f"Kalshi API {status} on {path}: {detail[:160]}")
        self.status = status


def _cents_to_prob(v) -> float | None:
    """Kalshi quotes YES in cents (1-99). Normalize to [0,1]; pass through if already a fraction."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x <= 0:
        return None
    return x / 100.0 if x > 1.0 else x


def _dollars(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if 0.0 < x < 1.0 else None


def parse_quote(m: dict) -> dict:
    """{bid, ask, last, mid, volume, volume_24h} for one raw market dict, in [0,1] probabilities.
    Dollar fields first (current API), cent fields as a fallback. mid = (bid+ask)/2 when two-sided, else last."""
    bid = _dollars(m.get("yes_bid_dollars"))
    ask = _dollars(m.get("yes_ask_dollars"))
    last = _dollars(m.get("last_price_dollars"))
    if bid is None and ask is None and last is None:
        bid, ask, last = (_cents_to_prob(m.get("yes_bid")), _cents_to_prob(m.get("yes_ask")),
                          _cents_to_prob(m.get("last_price")))
    mid = (bid + ask) / 2.0 if (bid is not None and ask is not None and ask >= bid) else last

    def _num(*keys):
        for k in keys:
            try:
                return float(m.get(k))
            except (TypeError, ValueError):
                continue
        return None
    return {"bid": bid, "ask": ask, "last": last, "mid": mid,
            "volume": _num("volume_fp", "volume"), "volume_24h": _num("volume_24h_fp", "volume_24h")}


class KalshiClient:
    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, timeout: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=timeout, transport=transport,
            headers={"Accept": "application/json", "User-Agent": "sig-bot/0.1 (+research)"})

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "KalshiClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            r = await self._client.get(path, params=params or {})
        except httpx.HTTPError as exc:
            raise KalshiError(0, path, f"transport:{type(exc).__name__}") from exc
        if r.status_code != 200:
            raise KalshiError(r.status_code, path, r.text)
        return r.json()

    async def get_market(self, ticker: str) -> dict | None:
        """One market by ticker -> the raw market dict (or None)."""
        d = await self._get(f"/markets/{ticker}")
        return d.get("market") if isinstance(d, dict) else None

    async def market_mid(self, ticker: str) -> float | None:
        """YES mid-probability for a market, in [0,1]. (yes_bid+yes_ask)/2, else last_price. None if no quote."""
        m = await self.get_market(ticker)
        if not m:
            return None
        return parse_quote(m)["mid"]

    async def markets_by_tickers(self, tickers: list[str], chunk: int = 40) -> dict[str, dict]:
        """{ticker: raw market} for many tickers in a few batched GET /markets?tickers=a,b,... calls."""
        out: dict[str, dict] = {}
        for i in range(0, len(tickers), chunk):
            part = tickers[i:i + chunk]
            d = await self._get("/markets", params={"tickers": ",".join(part), "limit": len(part)})
            for m in (d.get("markets", []) if isinstance(d, dict) else []):
                if m.get("ticker"):
                    out[m["ticker"]] = m
        return out

    async def list_markets(self, *, series_ticker: str | None = None, event_ticker: str | None = None,
                           status: str | None = None, limit: int = 200) -> list[dict]:
        """Markets under a series or event (for matching SIG races to Kalshi tickers)."""
        params: dict = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if status:
            params["status"] = status
        d = await self._get("/markets", params=params)
        return d.get("markets", []) if isinstance(d, dict) else []
