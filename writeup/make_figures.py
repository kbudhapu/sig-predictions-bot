"""Figures for the SIG Predictions Cup write-up. Run from the repo root: python writeup/make_figures.py

Inputs (writeup/data/, exported read-only from the droplet's state.db and leaderboard recorder on 2026-11-22):
  nav_history.csv          bot's own account snapshots from 2026-10-06 (cash, positions value, leaderboard P&L and rank)
  leaderboard_series.csv   every 15-min leaderboard snapshot from 2026-10-09: board size, my rank, P&L at ranks 1/3/10/top-10%
  orders_daily.csv         local orders per day and strategy
  orders_oct2.csv          the local order log for 2026-10-02 08:00-15:00 UTC
  sharp_gap_by_bucket.csv  SIG mid minus Polymarket price, by Polymarket price bucket (10% sample of per-minute rows, Oct 5 - Nov 3)
Plus the venue's daily /portfolio/history snapshots for Oct 2-6 (copied below), which predate nav_history.

PROVISIONAL: two markets (House and Senate control) were still unsettled on 2026-11-22. Re-export nav_history.csv after
settlement and re-run; the final-point annotations read the last row."""
import csv
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]; D = ROOT / "writeup" / "data"; OUT = ROOT / "writeup" / "figures"; OUT.mkdir(parents=True, exist_ok=True)
SURF, INK, INK2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1"
BLUE, ORANGE, AQUA, YELLOW, MAGENTA = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"
plt.rcParams.update({"figure.facecolor": SURF, "axes.facecolor": SURF, "savefig.facecolor": SURF, "font.size": 10.5,
                     "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
                     "axes.grid": True, "grid.color": GRID, "grid.linewidth": 1, "axes.spines.top": False,
                     "axes.spines.right": False, "axes.titleweight": "bold", "axes.titlesize": 12, "text.color": INK,
                     "font.family": "DejaVu Sans", "axes.axisbelow": True})
T = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
DFMT = mdates.DateFormatter("%b %d")


def save(fig, name):
    fig.savefig(OUT / name, dpi=200, bbox_inches="tight"); plt.close(fig); print("wrote", name)


nav = [r for r in csv.DictReader(open(D / "nav_history.csv")) if r["lb_pnl"] and r["lb_rank"]]
lb = list(csv.DictReader(open(D / "leaderboard_series.csv")))
# venue /portfolio/history daily snapshots (00:00 UTC), read 2026-11-20; account P&L
PH = [("2026-10-01T16:00:00+00:00", 0.0), ("2026-10-02T00:00:00+00:00", 3047), ("2026-10-03T00:00:00+00:00", -1966),
      ("2026-10-04T00:00:00+00:00", -9991), ("2026-10-05T00:00:00+00:00", -9666), ("2026-10-06T00:00:00+00:00", -11206)]
CLOSE = T("2026-11-04T17:00:00+00:00")

# ---------------------------------------------------------------- 1. P&L and rank over the whole competition
fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 6.6), sharex=True, gridspec_kw={"height_ratios": [1.5, 1]})
tn = [T(r["ts"]) for r in nav]; pn = [float(r["lb_pnl"]) for r in nav]; rk = [int(r["lb_rank"]) for r in nav]
a1.plot([T(t) for t, _ in PH], [v for _, v in PH], "o--", color=BLUE, lw=1.5, ms=5, mec=SURF, mew=1.5, alpha=0.8, label="daily account snapshots (venue)")
a1.plot(tn, pn, color=BLUE, lw=2, label="leaderboard P&L (bot's own record)")
a1.axhline(0, color=MUTED, lw=1)
for ax in (a1, a2):
    ax.axvline(CLOSE, color=ORANGE, lw=1.2); ax.axvspan(T("2026-10-01T16:00:00+00:00"), T("2026-10-06T00:00:00+00:00"), color="#fbe3d9", lw=0, alpha=0.6)
a1.text(T("2026-10-01T18:00:00+00:00"), 10300, "launch week:\nshipped untested", fontsize=8.5, color=INK2, va="top")
a1.annotate("day one +$3,047", (T(PH[1][0]), 3047), (T("2026-10-06T12:00:00+00:00"), 5600), fontsize=9, color=INK2, arrowprops=dict(arrowstyle="-", color=MUTED))
a1.annotate("trough -$12.4k", (T("2026-10-06T05:00:00+00:00"), -12021), (T("2026-10-10T00:00:00+00:00"), -12800), fontsize=9, color=INK2, arrowprops=dict(arrowstyle="-", color=MUTED))
ci = min(range(len(tn)), key=lambda i: abs((tn[i] - CLOSE).total_seconds()))
a1.annotate(f"market close -${abs(pn[ci]):,.0f}", (tn[ci], pn[ci]), (T("2026-10-24T00:00:00+00:00"), -9500), fontsize=9, color=INK2, arrowprops=dict(arrowstyle="-", color=MUTED))
a1.annotate(f"after settlement +${pn[-1]:,.0f}\n(House + Senate still unsettled)", (tn[-1], pn[-1]), (T("2026-11-08T00:00:00+00:00"), 2500),
            fontsize=9, color=INK, arrowprops=dict(arrowstyle="-", color=MUTED))
a1.text(CLOSE, 9800, " trading closed", fontsize=8.5, color=INK2, va="top")
a1.set_ylabel("P&L (SUSQies)"); a1.set_ylim(-15000, 11000); a1.legend(frameon=False, fontsize=9, loc="lower right")
a1.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v / 1000:+.0f}k" if v else "0"))
a2.plot([T(r["ts"]) for r in lb], [int(r["total"]) for r in lb], color=MUTED, lw=1.5, label="accounts on the leaderboard")
a2.plot(tn, rk, color=BLUE, lw=2, label="my rank")
a2.invert_yaxis(); a2.set_ylabel("rank (1 = top)"); a2.legend(frameon=False, fontsize=9, loc="center left")
a2.annotate(f"rank {rk[-1]:,} of {int(lb[-1]['total']):,}", (tn[-1], rk[-1]), (T("2026-11-08T00:00:00+00:00"), 300), fontsize=9, color=INK, arrowprops=dict(arrowstyle="-", color=MUTED))
a2.xaxis.set_major_formatter(DFMT)
fig.suptitle("SIG Predictions Cup: P&L and rank, Oct 1 to Nov 22 (provisional)", x=0.01, ha="left", fontsize=13, fontweight="bold")
save(fig, "fig1_pnl_rank.png")

# ---------------------------------------------------------------- 2. order activity by strategy per day
fam = lambda s: "exit (thesis / close)" if s.startswith("exit") else {"cross_market_divergence": "cross-market (core book)", "market_making": "market making",
                                                                    "sniper": "sniper"}.get(s, "other")
days = defaultdict(lambda: defaultdict(int))
for r in csv.DictReader(open(D / "orders_daily.csv")):
    if r["day"] <= "2026-11-05": days[r["day"]][fam(r["strategy"])] += int(r["orders"])
dl = sorted(days); X = [T(d + "T12:00:00+00:00") for d in dl]
order = [("market making", ORANGE), ("exit (thesis / close)", YELLOW), ("sniper", MAGENTA), ("cross-market (core book)", BLUE), ("other", AQUA)]
fig, ax = plt.subplots(figsize=(11, 3.8)); bot = np.zeros(len(dl))
for f, c in order:
    v = np.array([days[d][f] for d in dl]); ax.bar(X, v, bottom=bot, width=0.8, color=c, label=f, edgecolor=SURF, linewidth=0.6); bot += v
ax.set_ylabel("orders placed per day"); ax.xaxis.set_major_formatter(DFMT); ax.legend(frameon=False, fontsize=9, ncol=5, loc="upper right")
ax.set_title("Launch-week churn: most of my orders were placed in the first five days", loc="left")
ax.annotate("election night: limit orders cycling,\nmostly cancelled unfilled", (T("2026-11-04T12:00:00+00:00"), 2050), (T("2026-10-22T00:00:00+00:00"), 2700), fontsize=8.5, color=INK2, arrowprops=dict(arrowstyle="-", color=MUTED))
ax.text(0.01, -0.2, "local order table only; pilots after Oct 6 (rotation, fresh-gap, parity, election night) log orders separately",
        transform=ax.transAxes, fontsize=8.5, color=MUTED)
save(fig, "fig2_orders_by_day.png")

# ---------------------------------------------------------------- 3. the Oct 2 runaway, minute by minute
o2 = list(csv.DictReader(open(D / "orders_oct2.csv")))
fig, (b1, b2) = plt.subplots(2, 1, figsize=(10, 5.6), sharex=True, gridspec_kw={"height_ratios": [1, 1]})
w0, w1 = T("2026-10-02T11:35:00+00:00"), T("2026-10-02T12:45:00+00:00")
m842 = [(T(r["created_at"]), int(r["quantity"])) for r in o2 if r["exchange_id"] == "842" and r["strategy"] == "exit:842" and T("2026-10-02T11:50:00+00:00") <= T(r["created_at"]) <= T("2026-10-02T11:54:00+00:00")]
b1.plot([t for t, _ in m842], [q for _, q in m842], "o-", color=BLUE, lw=2, ms=6, mec=SURF, mew=1.5)
for t, q in m842[:6]: b1.annotate(f"{q:,}", (t, q), (6, 2), textcoords="offset points", fontsize=8.5, color=INK2)
b1.set_ylabel("shares per order"); b1.set_title("US Senate market: the exit re-buys in doubling sizes (11:51-11:52)", loc="left", fontsize=11)
mins = defaultdict(int)
for r in o2:
    t = T(r["created_at"])
    if r["strategy"].startswith("exit") and w0 <= t <= w1: mins[t.replace(second=0, microsecond=0)] += 1
mk = sorted(mins); b2.bar(mk, [mins[m] for m in mk], width=1 / 1440 * 0.85, color=YELLOW)
b2.set_ylabel("exit orders per minute"); b2.set_title("All markets: exit orders per minute", loc="left", fontsize=11)
for ax in (b1, b2):
    for t, lab, c in ((T("2026-10-02T11:57:00+00:00"), "Wikipedia ratings go live", MAGENTA), (T("2026-10-02T12:35:00+00:00"), "close path reverted", INK2)):
        ax.axvline(t, color=c, lw=1.5)
for t, lab in ((T("2026-10-02T11:57:00+00:00"), " Wikipedia ratings live 11:57"), (T("2026-10-02T12:35:00+00:00"), " close path reverted 12:35")):
    b2.text(t, b2.get_ylim()[1] * 0.92, lab, fontsize=8.5, color=INK2, va="top")
b2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M")); b2.set_xlim(w0, w1); b2.set_xlabel("Oct 2, UTC")
save(fig, "fig3_oct2_runaway.png")

# ---------------------------------------------------------------- 4. the favourite-longshot bias
g = list(csv.DictReader(open(D / "sharp_gap_by_bucket.csv")))
x = [int(r["bucket"]) * 0.05 + 0.025 for r in g]; y = [100 * float(r["mean_gap"]) for r in g]
fig, ax = plt.subplots(figsize=(9, 4))
ax.bar(x, y, width=0.04, color=[ORANGE if v > 0 else BLUE for v in y])
ax.axhline(0, color=MUTED, lw=1); ax.set_xlim(0, 1)
ax.set_xlabel("Polymarket price (probability of YES)"); ax.set_ylabel("SIG price minus Polymarket (cents)")
ax.text(0.30, 4.6, "longshots: SIG overprices (+3 to +5 cents)", fontsize=9, color=INK2, va="top")
ax.text(0.97, 1.6, "favourites: SIG underprices\n(-2 to -3 cents): what I bought", fontsize=9, color=INK2, ha="right", va="bottom")
ax.set_title("SIG's favourite-longshot bias against Polymarket", loc="left")
ax.text(0.0, -0.2, f"{sum(int(r['n']) for r in g):,} market-minutes (10% sample), Oct 5 - Nov 3; SIG mid vs Polymarket benchmark recorded each minute",
        transform=ax.transAxes, fontsize=8.5, color=MUTED)
save(fig, "fig4_favourite_longshot_bias.png")

# ---------------------------------------------------------------- 5. capital: cash vs positions
cash = [float(r["cash"]) for r in nav]; mv = [float(r["market_value"]) for r in nav]
fig, ax = plt.subplots(figsize=(11, 3.8))
ax.plot(tn, mv, color=BLUE, lw=2, label="positions (marked value)"); ax.plot(tn, cash, color=ORANGE, lw=2, label="cash")
ax.axvline(CLOSE, color=MUTED, lw=1)
ax.annotate("Nov 1: automated sell-down\nraises $40k for election night", (T("2026-11-01T01:00:00+00:00"), 40000), (T("2026-10-19T00:00:00+00:00"), 52000),
            fontsize=9, color=INK2, arrowprops=dict(arrowstyle="-", color=MUTED))
ax.annotate("settlement pays out", (T("2026-11-05T12:00:00+00:00"), 60000), (T("2026-11-09T00:00:00+00:00"), 75000), fontsize=9, color=INK2,
            arrowprops=dict(arrowstyle="-", color=MUTED))
ax.set_ylabel("SUSQies"); ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v / 1000:.0f}k"))
ax.xaxis.set_major_formatter(DFMT); ax.legend(frameon=False, fontsize=9, loc="center right")
ax.set_title("Capital-bound: cash sat near zero for most of October", loc="left")
save(fig, "fig5_cash_vs_positions.png")

# ---------------------------------------------------------------- 6-7. the core edge and the dropped ideas, tested against real settlements
# needs data/markets.csv (venue market list with settledWith) and data/hourly_prices.csv (hourly Polymarket + SIG mid per market)
import pandas as _pd
_m = _pd.read_csv(D / "markets.csv"); _S = dict(zip(_m.exchange_id, _m.settledWith.map({"YES": 1.0, "NO": 0.0})))
_h = _pd.read_csv(D / "hourly_prices.csv")
_h = _h[_h.hr.str.endswith("T15") & (_h.hr >= "2026-10-06") & (_h.hr < "2026-11-04")].dropna(subset=["sharp", "sig_mid"])
_h["S"] = _h.exchange_id.map(_S); _h = _h.dropna(subset=["S"]); _h = _h[(_h.sharp > 0.01) & (_h.sharp < 0.99)]
HS = 0.005; _g = _h.sharp - _h.sig_mid
def _sim(mask, side):
    x = _h[mask]; px = x.sig_mid + HS if side == "yes" else (1 - x.sig_mid) + HS; pay = x.S if side == "yes" else 1 - x.S
    return 100 * (pay - px).mean(), len(x)
STR = [("Core: buy the favourite when SIG is\n>= 5c below Polymarket", (_g >= 0.05) & (_h.sharp >= 0.5), "yes"),
       ("Core: buy NO on a longshot SIG prices\n>= 5c above Polymarket", (_g <= -0.05) & (_h.sharp < 0.5), "no"),
       ("Buy every SIG favourite (> 70%)", _h.sig_mid > 0.7, "yes"),
       ("Buy every SIG longshot (< 30%)\n(what early market making accumulated)", _h.sig_mid < 0.3, "yes"),
       ("Fade the benchmark: buy YES when SIG\nis >= 5c ABOVE Polymarket", _g <= -0.05, "yes")]
res = [(n,) + _sim(msk, sd) for n, msk, sd in STR]
fig, ax = plt.subplots(figsize=(9, 4.2))
ax.barh(range(len(res))[::-1], [r[1] for r in res], height=0.55, color=[BLUE if r[1] > 0 else ORANGE for r in res])
for i, (n, v, k) in enumerate(res):
    ax.text(v + (0.4 if v > 0 else -0.4), len(res) - 1 - i, f"{v:+.1f}c  (n={k:,})".replace("+-","-"), va="center", ha="left" if v > 0 else "right", fontsize=9, color=INK)
ax.set_yticks(range(len(res))[::-1]); ax.set_yticklabels([r[0] for r in res], fontsize=9); ax.grid(axis="y", visible=False)
ax.axvline(0, color=MUTED, lw=1); ax.set_xlim(-17, 14); ax.set_xlabel("profit per share held to settlement (cents, after half a 1c spread)")
ax.set_title("Held to the official settlement, the core rule earned ~9c a share", loc="left")
ax.text(0.0, -0.2, "one entry per market per day at 15:00 UTC, Oct 6 - Nov 3, at SIG's mid; outcomes from the venue's settlement records",
        transform=ax.transAxes, fontsize=8.5, color=MUTED)
save(fig, "fig6_edge_held_to_settlement.png")

bins = [0, .1, .3, .5, .7, .9, 1]
calP = _h.assign(b=_pd.cut(_h.sharp, bins)).groupby("b", observed=True).agg(p=("sharp", "mean"), won=("S", "mean"))
calS = _h.assign(b=_pd.cut(_h.sig_mid, bins)).groupby("b", observed=True).agg(p=("sig_mid", "mean"), won=("S", "mean"))
fig, ax = plt.subplots(figsize=(6.2, 5.6))
ax.plot([0, 1], [0, 1], color=MUTED, lw=1)
ax.plot(calP.p, calP.won, "-o", color=BLUE, lw=2, ms=7, mec=SURF, mew=1.5, label=f"Polymarket (Brier {((_h.sharp - _h.S) ** 2).mean():.4f})")
ax.plot(calS.p, calS.won, "-o", color=ORANGE, lw=2, ms=7, mec=SURF, mew=1.5, label=f"SIG (Brier {((_h.sig_mid - _h.S) ** 2).mean():.4f})")
ax.set_xlabel("price (implied probability); each venue binned by its own price"); ax.set_ylabel("share that actually won"); ax.set_xlim(0, 1); ax.set_ylim(0, 1)
ax.legend(frameon=False, fontsize=9, loc="upper left"); ax.set_title("2026 calibration: SIG's prices were pulled toward 50%", loc="left", fontsize=11)
ax.annotate(f"SIG under 10%: priced {100 * calS.p.iloc[0]:.1f}%,\nwon {100 * calS.won.iloc[0]:.1f}%", (calS.p.iloc[0], calS.won.iloc[0]), (0.30, 0.04), fontsize=8.5, color=INK2, arrowprops=dict(arrowstyle="-", color=MUTED))
ax.annotate(f"SIG over 90%: priced {100 * calS.p.iloc[-1]:.1f}%,\nwon {100 * calS.won.iloc[-1]:.1f}%", (calS.p.iloc[-1], calS.won.iloc[-1]), (0.22, 0.80), fontsize=8.5, color=INK2, arrowprops=dict(arrowstyle="-", color=MUTED))
ax.text(0.0, -0.16, "daily 15:00 UTC snapshots, Oct 6 - Nov 3; one national election, so outcomes are correlated across markets", transform=ax.transAxes, fontsize=8, color=MUTED)
save(fig, "fig7_calibration_2026.png")

# ---------------------------------------------------------------- 8. rolling 72h Sharpe (hourly returns of 100k + leaderboard P&L), Oct 6 onward
_n = _pd.read_csv(D / "nav_history.csv"); _n = _n[_n.lb_pnl.notna()]; _n["ts"] = _pd.to_datetime(_n.ts, format="ISO8601")
_v = (100000 + _n.set_index("ts").lb_pnl).resample("1h").last().ffill()
_v = _v[:CLOSE]; _r = _v.pct_change().dropna()   # trading period only: after the close the curve is settlement steps, not returns
_roll = _r.rolling(72, min_periods=48).apply(lambda x: x.mean() / x.std() * np.sqrt(24 * 365) if x.std() > 0 else np.nan)
_full = _r.mean() / _r.std() * np.sqrt(24 * 365)
fig, ax = plt.subplots(figsize=(11, 3.9))
ax.plot(_roll.index, _roll.values, color=BLUE, lw=2); ax.axhline(0, color=MUTED, lw=1)
ax.axhline(_full, color=ORANGE, lw=1.5, ls=(0, (4, 3))); ax.text(_roll.index[len(_roll) // 2], _full - 6, f"Oct 6 to close, whole period: {_full:.1f}", fontsize=9, color=INK2)
ax.set_ylabel("annualised Sharpe, rolling 72h"); ax.xaxis.set_major_formatter(DFMT)
ax.set_title("Rolling 72-hour Sharpe on hourly account returns, Oct 6 to market close", loc="left")
ax.text(0.0, -0.2, "hourly marked account value from the bot's own record; starts after launch week (the -$12.4k trough is not in this series); windows under 48 returns omitted",
        transform=ax.transAxes, fontsize=8.5, color=MUTED)
save(fig, "fig8_rolling_sharpe.png")

# ---------------------------------------------------------------- 9. where the final score came from: P&L step at each settlement batch
_b = _pd.read_csv(D / "settlement_batches.csv")
fig, ax = plt.subplots(figsize=(10, 3.9))
_x = np.arange(len(_b)); ax.bar(_x, _b.pnl_change, width=0.6, color=BLUE)
for i, r in _b.iterrows():
    ax.text(i, r.pnl_change + 150, f"{r.pnl_change:+,.0f}\n({r.markets} mkts)", ha="center", fontsize=8, color=INK)
ax.set_xticks(_x); ax.set_xticklabels([s.replace(" ", "\n") for s in _b.batch_start], fontsize=8.5); ax.grid(axis="x", visible=False)
ax.set_ylim(0, _b.pnl_change.max() * 1.25); ax.set_ylabel("P&L change across the batch")
ax.set_title(f"Settlement batches turned -\$4,644 at close into +\$8,849: {_b.pnl_change.sum():+,.0f} in total", loc="left")
ax.text(0.0, -0.24, "batch = markets the venue settled within 30 minutes of each other (UTC); change = leaderboard P&L 5 min before to 20 min after",
        transform=ax.transAxes, fontsize=8.5, color=MUTED)
save(fig, "fig9_settlement_batches.png")
