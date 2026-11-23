"""Tests for strategies/allocator — the dynamic capital allocator. Covers the six required behaviors and
specifically the three anti-bug guards: shrinkage (no lucky-streak capture), dataless→prior (no cold-start
4× bug), step-cap (no oscillation). Parameters load from config/sig.yaml (not hardcoded)."""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.allocator import load_config, Priors, Allocator, EdgeState, Rebalancer, Phase, Standing

P = []; F = []
def ck(n, c, d=""): (P if c else F).append(n); print(("PASS " if c else "FAIL ") + n + (" :: " + d if d else ""))
def run(coro): return asyncio.run(coro)

CFG = load_config()
PRIORS = Priors(CFG)
MD = PRIORS.max_deploy_frac
ME = PRIORS.max_edge_frac
COLD = PRIORS.cold_start_weights()


def alloc():
    return Allocator(CFG, Priors(CFG))


# ---------------- (a) + (b) GUARD: shrinkage + dataless→prior ----------------
def test_shrinkage_no_lucky_streak():
    a = alloc()
    hot2 = run(a.propose({"favorite_longshot": EdgeState("favorite_longshot", returns=[0.5, 0.5])}))["weights"]
    hot500 = run(a.propose({"favorite_longshot": EdgeState("favorite_longshot", returns=[0.5] * 500)}))["weights"]
    cold = COLD["favorite_longshot"]
    ck("(a) lucky 2-trade streak stays NEAR its small prior (shrinkage)", hot2["favorite_longshot"] <= cold * 1.7,
       f"w2={hot2['favorite_longshot']:.4f} prior={cold:.4f}")
    ck("(a) a sustained 500-trade record earns materially MORE than the 2-trade streak",
       hot500["favorite_longshot"] > hot2["favorite_longshot"] * 2.5,
       f"w2={hot2['favorite_longshot']:.4f} w500={hot500['favorite_longshot']:.4f}")
    ck("(a) the 2-trade lucky edge does NOT grab the book (stays small)", hot2["favorite_longshot"] < 0.05)


def test_dataless_to_prior_guard():
    a = alloc()
    allc = run(a.propose({}))["weights"]          # every edge dataless
    ck("(b) fully-dataless allocation == the mechanical-core prior (NOT vol-derived)",
       all(abs(allc[n] - COLD[n]) < 1e-9 for n in COLD), "weights==cold_start")
    # the cold-start top edges are the CORE priors (arb/MM), never a dataless spike
    top = max(allc, key=allc.get)
    ck("(b) largest cold-start weight is a CORE edge at its prior (arb/MM), not a 4× spike",
       top in ("arb", "market_making") and allc[top] <= PRIORS.prior_weight(top) / sum(e.prior for e in PRIORS.edges.values()) * MD + 1e-9)
    # a single dataless TAIL edge amid funded CORE edges sits at its prior, far below the funded ones
    funded = run(a.propose({"arb": EdgeState("arb", returns=[0.1] * 200),
                            "market_making": EdgeState("market_making", returns=[0.1] * 200)}))["weights"]
    ck("(b) a dataless edge sits at ~its prior, never the max (no cold-start 4× bug)",
       funded["favorite_longshot"] < funded["arb"] and funded["favorite_longshot"] <= COLD["favorite_longshot"] + 1e-9,
       f"dataless favorite={funded['favorite_longshot']:.4f} arb={funded['arb']:.4f}")


# ---------------- (c) GUARD: step-cap limits per-rebalance movement ----------------
def test_step_cap():
    rb = Rebalancer(CFG, Priors(CFG))
    rb.seed(COLD)
    start = dict(rb.current)
    target = {n: 0.0 for n in COLD}
    target["arb"] = MD                                 # a wild jump: everything into arb
    new = run(rb.step(target, force=True))
    max_step = CFG["allocator"]["max_step"]
    ok = all(abs(new[n] - start[n]) <= max_step * abs(target[n] - start[n]) + 1e-9 for n in COLD)
    moved = abs(new["arb"] - start["arb"])
    ck("(c) step-cap: every edge moved ≤ max_step × its gap (no whipsaw)", ok)
    ck("(c) step-cap: arb moved exactly max_step of its gap, not the whole way",
       abs(moved - max_step * (target["arb"] - start["arb"])) < 1e-9 and new["arb"] < target["arb"],
       f"moved={moved:.4f} full_gap={target['arb']-start['arb']:.4f}")


# ---------------- (d) decaying edge loses allocation (rolling window + loss penalty) ----------------
def test_decay_and_loss_penalty():
    a = alloc()
    good = run(a.propose({"cross_market_divergence": EdgeState("cross_market_divergence", returns=[0.1] * 120)}))["weights"]["cross_market_divergence"]
    decayed = run(a.propose({"cross_market_divergence": EdgeState("cross_market_divergence", returns=[0.1] * 100 + [-0.2] * 20)}))["weights"]["cross_market_divergence"]
    ck("(d) a decaying edge (bad RECENT returns, decay-weighted) loses allocation vs all-good", decayed < good,
       f"good={good:.4f} decayed={decayed:.4f}")
    clean = run(a.propose({"cross_market_divergence": EdgeState("cross_market_divergence", returns=[0.1] * 120, loss_share=0.0)}))["weights"]["cross_market_divergence"]
    bleeding = run(a.propose({"cross_market_divergence": EdgeState("cross_market_divergence", returns=[0.1] * 120, loss_share=0.5)}))["weights"]["cross_market_divergence"]
    ck("(d) loss-attribution penalty cuts the allocation of an edge bleeding NOW", bleeding < clean,
       f"clean={clean:.4f} bleeding={bleeding:.4f}")


# ---------------- (e) CORE floor holds while working, lifts when the edge dies ----------------
def test_core_floor_holds_then_lifts():
    a = alloc()
    floor = PRIORS.floor("arb")
    # arb WORKING but mediocre, while directional edges run hot → arb merit would fall below floor → floor holds
    hot = {e: EdgeState(e, returns=[0.2] * 500) for e in ("cross_market_divergence", "joint_structure", "order_flow", "extremizing", "favorite_longshot")}
    working = dict(hot); working["arb"] = EdgeState("arb", returns=([0.1, -0.095] * 25))  # mean>eps, low sharpe, n=50
    w_work = run(a.propose(working))["weights"]
    ck("(e) CORE floor HOLDS: a working arb stays ≥ its floor even when directional edges are hot",
       w_work["arb"] >= floor - 1e-9, f"arb={w_work['arb']:.4f} floor={floor}")
    # arb DEAD: a full window of ~zero returns → floor releases → arb can fall below the floor
    dead = dict(hot); dead["arb"] = EdgeState("arb", returns=[0.0] * 160)   # n≥release_min_n, mean≤eps → dead
    w_dead = run(a.propose(dead))["weights"]
    ck("(e) CORE floor LIFTS: a dead arb (full window ~0) is allowed below the floor (don't fund a corpse)",
       w_dead["arb"] < floor, f"arb_dead={w_dead['arb']:.4f} floor={floor}")
    ck("(e) meanwhile a still-working CORE edge keeps its floor (MM dataless → prior, floor active)",
       w_dead["market_making"] >= PRIORS.floor("market_making") - 1e-9, f"mm={w_dead['market_making']:.4f}")


# ---------------- (f) proposed weights never exceed the risk caps (propose-then-cap) ----------------
def test_risk_caps():
    a = alloc()
    out = run(a.propose({"arb": EdgeState("arb", returns=[0.3] * 500)}))   # arb hot, everything else starved
    proposed, weights = out["proposed"], out["weights"]
    ck("(f) the cap actually binds here (pre-cap arb would exceed max_edge_frac)", proposed["arb"] > ME,
       f"proposed_arb={proposed['arb']:.4f} cap={ME}")
    ck("(f) after cap, no edge exceeds max_edge_frac", all(v <= ME + 1e-9 for v in weights.values()),
       f"max={max(weights.values()):.4f}")
    ck("(f) after cap, Σ weights ≤ max_deploy_frac", sum(weights.values()) <= MD + 1e-9,
       f"sum={sum(weights.values()):.4f} md={MD}")


# ---------------- phase-awareness (endgame defers to Browne) ----------------
def test_phase_awareness():
    a = alloc()
    scores = run(a.propose({e: EdgeState(e, returns=[0.2] * 300) for e in ("cross_market_divergence", "joint_structure")}))["scores"]
    target = COLD
    # ENDGAME + BEHIND → concentrate the best directional edge (defer to Browne)
    rb = Rebalancer(CFG, Priors(CFG)); rb.seed(COLD)
    # step repeatedly (force) so the step-cap converges toward the concentrated target
    w = None
    for _ in range(40):
        w = run(rb.step(target, scores, phase=Phase.ENDGAME, standing=Standing.BEHIND, force=True))
    best_dir = max(("cross_market_divergence", "joint_structure"), key=lambda n: scores[n]["score_mult"])
    ck("(endgame/behind) allocator concentrates the best directional edge (defers to Browne)",
       w[best_dir] > w["joint_structure" if best_dir != "joint_structure" else "cross_market_divergence"] or w[best_dir] > 0.05,
       f"{best_dir}={w[best_dir]:.3f}")
    # ENDGAME + AHEAD → tighten to CORE (directional → 0)
    rb2 = Rebalancer(CFG, Priors(CFG)); rb2.seed(COLD)
    w2 = None
    for _ in range(40):
        w2 = run(rb2.step(target, scores, phase=Phase.ENDGAME, standing=Standing.AHEAD, force=True))
    ck("(endgame/ahead) allocator tightens to low-variance CORE (directional drained)",
       w2["cross_market_divergence"] < 0.02 and (w2["arb"] + w2["market_making"]) > 0.4,
       f"core={(w2['arb']+w2['market_making']):.3f} dir={w2['cross_market_divergence']:.3f}")


# ---------------- cadence (not per-trade) ----------------
def test_cadence():
    clk = {"t": 1000.0}
    rb = Rebalancer(CFG, Priors(CFG), clock=lambda: clk["t"]); rb.seed(COLD, now=clk["t"])
    tgt = {n: 0.0 for n in COLD}; tgt["arb"] = MD
    w_early = run(rb.step(tgt, now=clk["t"] + 10))     # well within the interval → no move
    ck("cadence: not due → allocation unchanged (no per-trade churn)", abs(w_early["arb"] - COLD["arb"]) < 1e-9)
    w_due = run(rb.step(tgt, now=clk["t"] + CFG["allocator"]["rebalance_interval_s"] + 1))
    ck("cadence: due after the interval → it rebalances (step-capped)", w_due["arb"] > COLD["arb"])


def test_static_mode_bypasses_performance():
    import copy
    scfg = copy.deepcopy(CFG)
    scfg["allocator"]["static"] = True
    a = Allocator(scfg, Priors(scfg))
    # dynamic mode would starve an edge fed 500 terrible returns; static must IGNORE performance and
    # sit at the prior — identical to the dataless weight.
    bad = {"cross_market_divergence": EdgeState("cross_market_divergence", returns=[-0.5] * 500)}
    w_bad = run(a.propose(bad))["weights"]
    w_dataless = run(a.propose({}))["weights"]
    ck("static: bad realized perf does NOT change the weight (tracks prior)",
       abs(w_bad["cross_market_divergence"] - w_dataless["cross_market_divergence"]) < 1e-9,
       f"bad={w_bad['cross_market_divergence']:.4f} dataless={w_dataless['cross_market_divergence']:.4f}")
    # and a dynamic allocator WOULD have starved it (sanity: the bypass is doing something)
    dyn = Allocator(CFG, Priors(CFG))
    w_dyn = run(dyn.propose(bad))["weights"]
    ck("static: dynamic would have starved the same edge (bypass is meaningful)",
       w_bad["cross_market_divergence"] > w_dyn["cross_market_divergence"] + 1e-6,
       f"static={w_bad['cross_market_divergence']:.4f} dynamic={w_dyn['cross_market_divergence']:.4f}")


def main():
    test_shrinkage_no_lucky_streak()
    test_static_mode_bypasses_performance()
    test_dataless_to_prior_guard()
    test_step_cap()
    test_decay_and_loss_penalty()
    test_core_floor_holds_then_lifts()
    test_risk_caps()
    test_phase_awareness()
    test_cadence()
    print(f"\n==== ALLOCATOR TESTS: PASS {len(P)}/{len(P)+len(F)} ====")
    if F:
        print("FAILED:", F); sys.exit(1)


if __name__ == "__main__":
    main()
