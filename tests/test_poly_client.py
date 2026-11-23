"""Verify the async Polymarket client OFFLINE via httpx.MockTransport — NO real HTTP, no key.

Covers: gamma market parsing (JSON-encoded array fields → lists/floats), yes_price/yes_token_id,
CLOB midpoint/price parsing, get_market_probability preferring the CLOB midpoint, and 429/Retry-After
backoff-then-success.
"""
import os
import sys
import json

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals.poly_client import PolymarketClient, Market, PolymarketError

# A realistic gamma market: outcomes / outcomePrices / clobTokenIds are JSON-encoded STRINGS.
GAMMA_MARKET = {
    "id": "123",
    "question": "Which party controls the Senate after 2026?",
    "slug": "which-party-will-control-the-senate-after-the-2026-election",
    "outcomes": json.dumps(["Yes", "No"]),
    "outcomePrices": json.dumps(["0.71", "0.29"]),
    "clobTokenIds": json.dumps(["tok-yes", "tok-no"]),
    "closed": False,
    "active": True,
    "volume": "123456.7",
    "liquidity": "9000",
}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/markets"):
        return httpx.Response(200, json=[GAMMA_MARKET])
    if path.endswith("/midpoint"):
        return httpx.Response(200, json={"mid": "0.73"})
    if path.endswith("/price"):
        return httpx.Response(200, json={"price": "0.71"})
    return httpx.Response(404, json={"error": "not found"})


def test_market_parsing_pure():
    # the parse layer is pure — testable without a client
    m = Market.from_gamma(GAMMA_MARKET)
    assert m.outcomes == ["Yes", "No"]
    assert m.outcome_prices == [0.71, 0.29]
    assert m.clob_token_ids == ["tok-yes", "tok-no"]
    assert m.yes_price == 0.71
    assert m.yes_token_id == "tok-yes"
    assert m.token_for("no") == "tok-no"  # case-insensitive
    assert m.volume == 123456.7


@pytest.mark.asyncio
async def test_get_markets_parses_json_encoded_fields():
    async with PolymarketClient(transport=httpx.MockTransport(_handler)) as c:
        markets = await c.get_markets(slug="x")
    assert len(markets) == 1
    m = markets[0]
    assert m.id == "123" and m.question.startswith("Which party")
    assert m.outcomes == ["Yes", "No"]
    assert m.outcome_prices == [0.71, 0.29]
    assert m.yes_token_id == "tok-yes"


@pytest.mark.asyncio
async def test_clob_midpoint_and_price():
    async with PolymarketClient(transport=httpx.MockTransport(_handler)) as c:
        assert await c.get_midpoint("tok-yes") == 0.73
        assert await c.get_price("tok-yes", side="buy") == 0.71


@pytest.mark.asyncio
async def test_probability_prefers_clob_midpoint_over_gamma():
    m = Market.from_gamma(GAMMA_MARKET)  # gamma yes price = 0.71
    async with PolymarketClient(transport=httpx.MockTransport(_handler)) as c:
        p = await c.get_market_probability(m, "Yes", prefer_clob=True)
    assert p == 0.73  # the freshest CLOB midpoint, not the gamma 0.71


@pytest.mark.asyncio
async def test_probability_falls_back_to_gamma_when_clob_fails():
    def failing_clob(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/midpoint"):
            return httpx.Response(500, json={"error": "down"})
        return _handler(request)

    m = Market.from_gamma(GAMMA_MARKET)
    async with PolymarketClient(transport=httpx.MockTransport(failing_clob), max_retries=1, backoff_base=0.0) as c:
        p = await c.get_market_probability(m, "Yes", prefer_clob=True)
    assert p == 0.71  # fell back to gamma last-trade price


@pytest.mark.asyncio
async def test_429_retry_after_then_success():
    state = {"calls": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/markets"):
            state["calls"] += 1
            if state["calls"] == 1:
                return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate"})
            return httpx.Response(200, json=[GAMMA_MARKET])
        return _handler(request)

    async with PolymarketClient(transport=httpx.MockTransport(flaky), max_retries=3, backoff_base=0.0) as c:
        markets = await c.get_markets()
    assert state["calls"] == 2            # retried once after the 429
    assert markets[0].id == "123"


@pytest.mark.asyncio
async def test_429_exhausts_retries_raises():
    def always_429(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate"})

    async with PolymarketClient(transport=httpx.MockTransport(always_429), max_retries=2, backoff_base=0.0) as c:
        try:
            await c.get_markets()
        except PolymarketError as e:
            assert e.status == 429
            return
    assert False, "expected PolymarketError after exhausting retries"
