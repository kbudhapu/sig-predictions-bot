"""MARKET OWNERSHIP — exclusive per-market ownership for strategies that must be isolated (2026-10-08).

Why: SIG nets YES and NO in the same market at the venue (a buy-NO cancels a held YES). Two strategies trading the
same market therefore silently change each other's positions, and the orchestrator's exits act on EVERY held
position. The only robust isolation is exclusivity: an owned market is traded by its owner and nobody else, so the
venue position in it IS the owner's position (exact attribution, no netting across strategies).

Enforced at the broker chokepoint (SigBroker.place_order / place_multi_leg / cancel_order), so no code path —
entries, exits, MM, arb, chase, timeouts — can bypass it. Unowned markets behave exactly as before.

Persisted (atomic JSON write) so ownership survives restarts: an owner keeps its markets while it still holds
inventory, even if its loop is switched off. Only the owner can release a market.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Iterable

DEFAULT_PATH = "data/market_ownership.json"
# Strategies that may trade ONLY markets they own (two-way isolation: nobody else in their markets, and they
# never trade anywhere else).
ISOLATED_STRATEGIES = frozenset({"fair_band", "fresh_gap", "rotation", "election_night", "parity"})


def _replace(tmp: str, target: str, tries: int = 6) -> None:
    """os.replace, retrying a TRANSIENT PermissionError: on Windows an AV scanner / indexer briefly holds a just-written
    file open and the rename fails with WinError 5 for a few ms. Linux (the droplet) never hits this. A denial that
    persists past the retries still raises, so callers keep their fail-safe behaviour (claim() undoes)."""
    import time as _t
    for i in range(tries):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            _t.sleep(0.01 * (i + 1))


class MarketOwnership:
    def __init__(self, path: str | None = DEFAULT_PATH, isolated: frozenset = ISOLATED_STRATEGIES) -> None:
        self._path = path
        self._isolated = frozenset(isolated)
        self._lock = threading.Lock()
        self._owner: dict[str, str] = {}          # exchange_id -> strategy_id
        self._orders: dict[str, str] = {}         # venue order id -> owning strategy (owned strategies only)
        self._claimed_at: dict[str, str] = {}     # exchange_id -> ISO time of the claim (P&L counts fills after it)
        self._beat: dict[str, str] = {}           # owner strategy -> ISO time of its last heartbeat (LEASE)
        self._order_ex: dict[str, str] = {}       # venue order id -> exchange (release only when none remain)
        self._history: list = []                  # [exchange, strategy, claimed_at, released_at|None] (loss scope)
        self._last_reload = 0.0
        self.corrupt = False                      # unreadable file: owners must refuse to run (see fair_band_live)
        self._load()

    # ---- persistence ----
    def _load(self) -> None:
        """Load the registry, falling back to the .bak copy. If a file EXISTS but neither copy is readable the
        registry is CORRUPT and fails CLOSED for everyone (allows() -> False: no new orders anywhere; cancels stay
        allowed) — failing open would let every strategy trade into markets an isolated strategy holds."""
        if not self._path:
            return
        paths = [p for p in (self._path, self._path + ".bak") if os.path.exists(p)]
        if not paths:
            return
        for path in paths:
            try:
                with open(path, encoding="utf-8") as fh:
                    d = json.load(fh)
                self._owner = {str(k): str(v) for k, v in (d.get("markets") or {}).items()}
                self._orders = {str(k): str(v) for k, v in (d.get("orders") or {}).items()}
                self._claimed_at = {str(k): str(v) for k, v in (d.get("claimed_at") or {}).items()}
                self._beat = {str(k): str(v) for k, v in (d.get("heartbeat") or {}).items()}
                self._order_ex = {str(k): str(v) for k, v in (d.get("order_ex") or {}).items()}
                self._history = [list(h) for h in (d.get("history") or []) if isinstance(h, (list, tuple))]
                self.corrupt = False
                return
            except Exception:  # noqa: BLE001
                continue
        self._owner, self._orders, self._claimed_at = {}, {}, {}
        self.corrupt = True

    def _save(self) -> None:
        if not self._path:
            return
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        payload = json.dumps({"markets": self._owner, "orders": self._orders, "claimed_at": self._claimed_at,
                              "heartbeat": self._beat, "order_ex": self._order_ex, "history": self._history[-500:]})
        for target in (self._path, self._path + ".bak"):           # atomic write of BOTH copies
            tmp = target + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            _replace(tmp, target)

    # ---- queries ----
    def owner_of(self, exchange_id) -> str | None:
        return self._owner.get(str(exchange_id))

    def owned_by(self, strategy_id: str) -> set[str]:
        return {ex for ex, s in self._owner.items() if s == strategy_id}

    def allows(self, strategy_id, exchange_id) -> bool:
        """May `strategy_id` place an order in `exchange_id`? Owned market: only its owner. Unowned market: anyone
        except an isolated strategy (unchanged behaviour for every existing strategy)."""
        sid = str(strategy_id)
        if self.corrupt:
            self._try_recover()
        if self.corrupt:
            # fail CLOSED for new exposure, but risk-REDUCING exits stay allowed everywhere (a frozen book is worse)
            return sid.startswith("exit:")
        o = self._owner.get(str(exchange_id))
        if o is None:
            return sid not in self._isolated
        if o == sid:
            return True
        # LEASE: an owner that stopped heart-beating (crashed / disabled / not started) no longer manages its markets
        # -> the orchestrator's exits may risk-manage what it left behind. New entries by others stay refused.
        return sid.startswith("exit:") and not self.lease_ok(o)

    def _try_recover(self) -> None:
        """A corrupt registry re-reads its files at most once a minute, so fixing the file recovers without a
        restart (until then: no new orders anywhere, exits and cancels allowed)."""
        import time as _t
        if _t.monotonic() - self._last_reload < 60.0:
            return
        self._last_reload = _t.monotonic()
        self._load()

    def orders_in(self, strategy_id: str, exchange_id) -> list[str]:
        return [oid for oid, s in self._orders.items()
                if s == strategy_id and self._order_ex.get(oid) == str(exchange_id)]

    def history_of(self, strategy_id: str) -> list:
        return [h for h in self._history if len(h) >= 3 and h[1] == strategy_id]

    LEASE_S = 600.0

    def beat(self, strategy_id: str, now: datetime | None = None) -> None:
        """Heartbeat: the owner is alive and managing its markets (persisted, so restarts see it)."""
        with self._lock:
            self._beat[str(strategy_id)] = (now or datetime.now(timezone.utc)).isoformat()
            self._save()

    def lease_ok(self, strategy_id: str, now: datetime | None = None) -> bool:
        ts = self._beat.get(str(strategy_id))
        if not ts:
            return False
        try:
            age = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(ts)).total_seconds()
        except (TypeError, ValueError):
            return False
        return age <= self.LEASE_S

    def managed_owner(self, exchange_id) -> str | None:
        """Owner of an owned market whose lease is LIVE (it is being managed), else None."""
        o = self._owner.get(str(exchange_id))
        return o if (o is not None and self.lease_ok(o)) else None

    def orders_of(self, strategy_id: str) -> list[str]:
        return [oid for oid, s in self._orders.items() if s == strategy_id]

    def claimed_at(self, exchange_id) -> str | None:
        return self._claimed_at.get(str(exchange_id))

    def order_owner(self, order_id) -> str | None:
        return self._orders.get(str(order_id))

    def allows_cancel(self, strategy_id, order_id) -> bool:
        """Orders placed by an owning strategy can only be cancelled by that strategy. Everything else as before."""
        o = self._orders.get(str(order_id))
        return o is None or o == str(strategy_id)

    # ---- mutations ----
    def claim(self, strategy_id: str, exchange_ids: Iterable) -> list[str]:
        """Claim unowned markets for `strategy_id`. Never steals; returns the markets now owned by it."""
        with self._lock:
            got, fresh = [], []
            for ex in exchange_ids:
                ex = str(ex)
                if self._owner.get(ex) in (None, strategy_id):
                    if self._owner.get(ex) is None:
                        self._claimed_at[ex] = datetime.now(timezone.utc).isoformat()
                        fresh.append(ex)
                        self._history.append([ex, strategy_id, self._claimed_at[ex], None])
                    self._owner[ex] = strategy_id
                    got.append(ex)
            try:
                self._save()
            except Exception:
                # not persisted -> would silently vanish on restart while we may hold inventory: undo + raise
                for ex in fresh:
                    self._owner.pop(ex, None)
                    self._claimed_at.pop(ex, None)
                self._history = [h for h in self._history if not (h[0] in fresh and h[3] is None
                                                                   and h[1] == strategy_id)]
                raise
            return got

    def release(self, strategy_id: str, exchange_id) -> bool:
        with self._lock:
            if self._owner.get(str(exchange_id)) != strategy_id:
                return False
            del self._owner[str(exchange_id)]
            self._claimed_at.pop(str(exchange_id), None)
            for h in self._history:
                if h[0] == str(exchange_id) and h[1] == strategy_id and h[3] is None:
                    h[3] = datetime.now(timezone.utc).isoformat()
            self._save()
            return True

    def note_order(self, strategy_id: str, order_id, exchange_id) -> None:
        """Record an order placed by an OWNING strategy (only for markets it owns)."""
        if order_id is None or self._owner.get(str(exchange_id)) != strategy_id:
            return
        with self._lock:
            self._orders[str(order_id)] = strategy_id
            self._order_ex[str(order_id)] = str(exchange_id)
            self._save()

    def forget_orders(self, order_ids: Iterable) -> None:
        with self._lock:
            for oid in order_ids:
                self._orders.pop(str(oid), None)
                self._order_ex.pop(str(oid), None)
            self._save()


_REGISTRY: MarketOwnership | None = None


def registry() -> MarketOwnership:
    """Process-wide registry (lazy). Tests swap it with set_registry()."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = MarketOwnership(os.environ.get("SIG_OWNERSHIP_PATH", DEFAULT_PATH))
    return _REGISTRY


def set_registry(reg: MarketOwnership | None) -> None:
    global _REGISTRY
    _REGISTRY = reg
