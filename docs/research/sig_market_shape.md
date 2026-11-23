# How the SIG market behaves (study of 2026-10-06)

Data: snapshot of `data/state.db`. SIG per-minute mids for 239 markets, 10-03 11:00Z to 10-06 08:06Z (2.9 days).
Polymarket ("sharp") alongside SIG for 196 markets over the last 16h. Spreads and depth are a snapshot taken now.
Scripts: `tools/research/shape.py`, `shape2.py`, `gbm.py` (run locally on a snapshot, never on the droplet).
Raw output: `sig_market_shape_raw_2026-10-06.txt`, `sig_gbm_raw_2026-10-06.txt`.
Caveat: 3 days is one regime (opening week). Re-run after ~10-13.

## 1. What the market is
- 239 markets: 92 House legs, 66 Senate, 66 Governor, 4 chamber control, 11 unmapped. 114 races have both a D and an R leg.
- Mostly lopsided. 121 of 239 markets are below 15c or above 85c; only 28 are in the 35-65c range.
- Tight and thick at the top of the book. 84% of markets sit at a one-tick spread (0.5c). Median depth at the
  best price is about 3-4k shares.

## 2. How it moves: a busy-looking market that is mostly noise
- A market's mid changes in about 10% of minutes. Moves are almost always 1 tick or less: 70% exactly 1 tick,
  24% half a tick (one side of the book moving), under 5% bigger.
- Over 60 minutes the median market doesn't move at all (p90 = 1c, p99 = 2.5c). Over 24h: median 1.5c, p99 7.5c.
- Path vs range: in 3 days the median market travelled 127c of up-and-down but ended within a 6.5c range,
  so 95% of all movement cancels out.
- Every horizon shows reversion. Autocorrelation of changes is negative from 1 minute (-0.20) to 60 minutes (-0.06).
  Variance ratios are well under 1 and keep falling: 0.66 at 5m, 0.43 at 60m, 0.32 at 12h.
  SIG prices are a slow "true" level plus a lot of temporary noise from participants pushing them around.
- Big jumps (≥3c in 5 min, 164 in 3 days) are usually partly given back: median 0.5-1c, with 60-65% giving
  something back within 15m to 12h.

## 3. When it moves
- Activity is decaying: 16.5% of market-minutes had a change on 10-03 (Sat, 3rd day of the cup), then 10.7% on
  10-04, then 6% on 10-05 and 10-06. Expect it to pick up again as the election nears.
- By hour (UTC): quietest 01-05Z (US night, ~5% of minutes); 60-min moves are largest 12-22Z (US day), about
  0.4c vs 0.3c at night. Night isn't more mispriced (see the earlier entry-policy test). It's just slower.
- By office: Senate and Governor move most (60-min |change| ~0.4c), House least (0.24c). Chamber-control
  markets rarely move.

## 4. How SIG relates to itself
- Races are independent. PCA of 60-min changes: the first factor explains only 6% of variance, and different
  races' legs have a median correlation of +0.003. There is no "national swing" factor. Each market
  moves on its own flow.
- The two legs of one race barely talk to each other. The correlation of 15-min changes between a race's
  R and D legs is -0.12, where an efficient market would be -1.00. People trade one leg without
  repricing the other.
- So D+R prices drift off adding up to 100%: the mean sum is +0.43c over 100% (median +0.75c); off by more
  than 2c 28% of the time, by more than 4c 3.8%.
  - Persistent offenders: VA-01 +2.8c, NC-Sen +2.8c, PA-Gov +2.7c; FL-20 -2.7c, CT-Gov -2.2c.
    (NE-Sen at -22c is a three-way race, which is expected.)
  - Deviations close. Half-life is about 2h. Of deviations of 4c or more, 87% close by an average of 1.7c
    within 4h.
- **This is the strongest predictive signal found** (see 6). Its downside is also guaranteed: buying both
  legs when they sum below 100%, or both NOs when they sum above, pays exactly 1 at settlement.

## 5. How SIG relates to Polymarket
- Polymarket is about 4x calmer than SIG: 60-min |change| 0.05c vs 0.22c.
- The gap (Polymarket minus SIG) is a stable per-market bias, not a wiggle. 96% of gap variance is the
  market's own persistent level. The per-market mean gap in the first 8h vs the last 8h correlates at +0.96.
  The transient wiggle around that level has a std of only 0.8c and a half-life of about 3h.
- The bias is the favourite-longshot pattern. SIG overprices underdogs (markets under 30c sit 3.7-4.1c above
  Polymarket) and underprices favourites (markets over 70c sit about 3c below). The middle is close to fair (-0.8c).
  That is exactly why buying NO on the underdog is the best-performing leg type.
- SIG barely follows Polymarket on a 4h timescale. When Polymarket moves 1c or more in 15 min, SIG follows only
  +0.24c within 60-240 min. When the gap is 5c or more, SIG closes only 0.09c of it over the next 4h.
  Cross-lag correlations are about 0 (+0.02 at most).
- Implication: cross-market is a settlement edge, not a quick-convergence trade. The money is in holding
  the biased side to Nov 4 (or until bias fades) and in book_edge (now logged). The 5c/2c band only pays where
  a market's persistent bias sits near the threshold, so the 0.8c wiggle crosses it.

## 6. Can a model predict SIG? (Numerai-style GBM)
- Setup: one row per market every 30 min. Targets: next 60m and 240m SIG change. Features: past changes
  (5m-12h), z-score vs 60m mean, distance from 4h/24h means, price level, activity, volatility, hour, office,
  party, D+R parity and its change, and the Polymarket gap (16h subset only). Eras are 2h blocks. Train on
  the first 60% of time, test on the rest, with a gap between them equal to the horizon.
- Result: the market is predictable in correlation terms but not in cents.

  | target | GBM era-corr (sharpe) | best one-feature baseline |
  |---|---|---|
  | 60m | +0.092 (1.28) | -z60 +0.100 (1.73) |
  | 240m | +0.048 (0.80) | -parity +0.114 (2.12) |

  - The GBM does not beat simple reversion rules. Top features are parity, price level, distance from the
    24h mean, and hour.
  - In cents, the top and bottom 10% of predictions move only +0.09c (60m) to +0.17c (240m) per share.
    Hit rates are 28-33%, because most hours have no move at all.
  - Taking the price on any of these signals costs more (0.5c round trip at a one-tick spread) than it earns.
  - With the Polymarket gap (16h, tiny sample) nothing holds up. The gap does not predict 1-4h moves (era-corr
    -0.04 to -0.09), consistent with 5.
- Verdict: short-horizon prediction is only monetisable passively (being the resting order, earning the
  spread on noise reversion). We tried that as market making and got picked off. A bigger model on these
  features will not change that. Data volume and regime are the limits, not the model.

## 7. What this says to do
1. Treat cross-market as a hold-to-settlement bias trade. Size by the persistent per-market gap, prefer the
   leg that bias favours (NO on overpriced underdogs), and stop expecting quick convergence. Rotation should
   compare remaining gap, not time held.
2. Test D+R parity reversion as a strategy, which is new, ranked above the GBM. Buy the cheap pair when the sum
   deviates by 3-4c or more. Exit when it reverts (about 2h half-life). If it doesn't revert, hold to
   settlement, which pays exactly 1, so the trade has no directional risk. Backtest with real bid/ask
   (sharp_history has them since 10-06 05:15Z) before any shadow.
3. Don't build directional short-term models: the edge per trade is under the spread.
4. Re-run this study around 10-13 with about 10 days of data, to check whether the decaying activity has
   changed any of it.
