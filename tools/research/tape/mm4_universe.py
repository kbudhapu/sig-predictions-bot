"""MM-4: two-sided skewed MM restricted to the 'MM-viable' universe (queues clear < 2h both sides, two-way flow,
|gap| < 2.5c) found 2026-10-10. Reuses mm3b.run2 machinery with a market filter."""
import os, sys, warnings
warnings.filterwarnings("ignore")
import pandas as pd
import mm3_twosided as M
import mm3b as B
U = set(pd.read_csv(os.path.join(os.environ["TEMP"], "mm_universe.csv"), dtype={"ex": str}).ex.astype(str))
M.tape = M.tape[M.tape.ex.astype(str).isin(U)]
print("universe:", sorted(U), "trades", len(M.tape))
for inv in (500.0, 1500.0):
    for Q in (200.0, 500.0):
        for pess in (True, False):
            r = B.run2("all", 0, Q, inv, 0.01, pess)
            print(f"inv ${inv:5.0f} Q ${Q:4.0f} {'pess' if pess else 'opt '}: @mid {r['mid']:7,.0f}/d @poly {r['poly']:7,.0f}/d "
                  f"vol {r['vol']:9,.0f}/d max cap {r['maxcap']:7,.0f} avg cap {r['avgcap']:7,.0f} fills {r['fills']:6,.0f}/d")
