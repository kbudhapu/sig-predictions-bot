"""P(reach the top-10% line) for the live book vs positive-EV, higher-variance alternatives (rank, not EV)."""
import numpy as np
exec(open("swing.py", encoding="utf-8").read().split('print("\nSettlement P&L from now')[0])
NEED = (15000, 17600, 20000)
px_h = float(mid["840"]); p_h = float(sharp["840"])          # House Dem control: SIG vs Poly
px_s = float(mid["843"]); p_s = float(sharp["843"])          # Senate Rep control
def odds(label, pnl):
    print(f"  {label:44s} mean {pnl.mean():7,.0f} sd {pnl.std():7,.0f} | " +
          "  ".join(f"P(>= +{n/1000:.1f}k) {np.mean(pnl >= n):.3f}" for n in NEED))
for rho in (0.3, 0.6):
    print(f"\nrho={rho}  (need ~+17.6k from now: lb -11.6k -> top-10% line ~+6k)")
    Z, base = simulate(rho); odds("current book", base)
    for h in (10000, 20000, 40000):
        Z, pnl = simulate(rho, (h, px_h, p_h))
        odds(f"+ House-Dem-control YES {h:,} sh (${h*px_h:,.0f})", pnl)
    for h in (10000, 20000):
        Z, pnl = simulate(rho, (-h, 1 - px_s, 1 - p_s))
        odds(f"+ Senate-Rep-control YES {h:,} sh (${h*px_s:,.0f}) [hedge]", pnl)
