# order_flow — fade "dumb" one-sided book pressure. Registered, INERT

strategy_id `order_flow`, tier SIGNAL, strategy_type `order_flow` (directional sizer, CHASE fills), cadence 5s.
Lifetime: 1 order. Open positions: none attributable.

## Files
- `strategies/order_flow/flow.py`: windowed flow book (imbalance over `window_seconds`).
- `strategies/order_flow/signal.py`: classification: QUIET / DUMB (fade) / INFORMED (never fade) / UNCONFIRMED (no trade).
- `CoreOrderFlowStrategy` in `sig/engine/live_strategies.py`.
- Data: `_orderbook_pump` (main_sig) polls `GET /exchanges/{id}/orderbook` for `order_flow.markets` every
  `book_poll_interval_s` (60s) and also feeds the sniper and book_pressure. The WS feed is NBBO-only, without sizes.

## Logic
1. Total resting bid qty and ask qty from the L2 book are ingested as pseudo "buy"/"sell" flow.
   (This is resting DEPTH, not the trade tape.)
2. An extreme one-sided imbalance (`extreme_threshold` 0.6, `min_volume` 200, `min_trades` 3) is a candidate.
3. The informed reference is **mid momentum**: the sign of the mid move over 45s if |move| ≥ 0.01.
   It is NOT the Polymarket sharp.
   - Flow opposing a real move → DUMB → fade it.
   - Flow agreeing → INFORMED → stand aside.
   - No move → UNCONFIRMED → no trade.
4. A fade BUY lifts the ask; a fade SELL hits the bid, and can only reduce a held YES (the gate rejects naked sells).

## Why it's inert
- Election markets rarely move ≥1c in 45s, so ~100% of extremes are UNCONFIRMED and `emit=False`.
- Not pool-eligible, so even a signal sizes to zero while cash < $15k reserve.
- Research (book_pressure validation) found ~0 predictive value in queue imbalance on this slow venue at 1–15 min.
  Neither fading nor following has an edge.

## Live config
`order_flow: enabled true, stream_sizes false, max_markets 8, book_poll_interval_s 60,
markets [843,842,841,840,948,947,983,856]`. Allocator prior 0.02. Kelly mult 1.0.
Its `markets` list also defines the bounded L2 set for the sniper/book_pressure and the mm_shadow set.

## Verdict
Dead weight, but harmless and cheap (~8 reads/min). Keep only as the L2-book pump owner, or disable.
