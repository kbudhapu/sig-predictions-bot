import numpy as np
exec(open("swing.py", encoding="utf-8").read().split('print("\nSettlement P&L from now')[0])
best = sum(max(r.dem_payoff, r.rep_payoff) - r.mark for r in P.itertuples())
print(f"BEST possible settlement P&L of the current book (every race goes our way): ${best:,.0f}")
px_s = float(mid["843"]); p_s = float(sharp["843"])
print(f"Senate-Rep-control: SIG {px_s:.3f} vs Poly {p_s:.3f}")
for rho in (0.3, 0.6):
    print(f"rho={rho}")
    for h in (0, 20000, 30000, 40000, 60000):
        Z, pnl = simulate(rho, (-h, 1 - px_s, 1 - p_s)) if h else simulate(rho)
        print(f"  Senate-Rep YES {h:6,d} sh (${h*px_s:7,.0f}): mean {pnl.mean():7,.0f} sd {pnl.std():7,.0f} | "
              + "  ".join(f"P>=+{n/1000:.1f}k {np.mean(pnl >= n):.3f}" for n in (17600, 20000, 25000)))
