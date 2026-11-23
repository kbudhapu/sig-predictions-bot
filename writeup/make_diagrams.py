"""Pipeline and architecture diagrams for the two projects (recruiter kit; not part of the write-ups).
Run: python make_diagrams.py  -> PNGs in figures/. Drawn from the system design, not from data."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
from pathlib import Path

OUT = Path(__file__).parent
NAMES = {'sig_architecture': 'figures/fig10_system_architecture', 'sig_pipeline': 'figures/fig11_pipelines'}
INK, MUTED, LINE = "#1f2933", "#52606d", "#7b8794"
PAL = {  # (fill, edge)
    "data": ("#e8f1fb", "#2f6fb0"), "train": ("#e9f6ee", "#2e8a4f"), "eval": ("#fdf4e3", "#b7791f"),
    "eng": ("#f0ebfb", "#6b46c1"), "ship": ("#fcebee", "#c53050"), "gray": ("#f3f5f7", "#7b8794"),
    "ext": ("#f3f5f7", "#52606d"), "cont": ("#fbfcfd", "#9aa5b1"),
}
plt.rcParams.update({"font.family": "DejaVu Sans", "svg.fonttype": "none"})


def canvas(w, h):
    fig = plt.figure(figsize=(w, h))
    ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, w); ax.set_ylim(0, h); ax.axis("off")
    return fig, ax


def box(ax, x, y, w, h, title=None, body=(), kind="gray", fs=9.3, tfs=11, dashed=False, lw=1.4, align="left", lh=0.255, z=2):
    f, e = PAL[kind]
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.10", fc=f, ec=e, lw=lw,
                                ls=(0, (4, 3)) if dashed else "-", zorder=z))
    ty = y + h - 0.2
    if title:
        ax.text(x + w / 2, ty, title, ha="center", va="top", fontsize=tfs, weight="bold", color=e if kind not in ("gray", "ext", "cont") else INK, zorder=z + 1)
        ty -= 0.42 if tfs >= 11 else 0.36
    for ln in body:
        cont = ln.startswith("  ")
        if align == "center":
            ax.text(x + w / 2, ty, ln.strip(), ha="center", va="top", fontsize=fs, color=INK, zorder=z + 1)
        else:
            ax.text(x + (0.32 if cont else 0.16), ty, ("" if cont else "• ") + ln.strip(), ha="left", va="top", fontsize=fs, color=INK, zorder=z + 1)
        ty -= lh
    return (x, y, w, h)


def arrow(ax, p, q, label=None, rad=0.0, dashed=False, color=LINE, fs=8.6, loff=(0, 0.13), lw=1.5, ha="center", both=False):
    ax.add_patch(FancyArrowPatch(p, q, arrowstyle="<|-|>" if both else "-|>", mutation_scale=13, lw=lw, color=color,
                                 connectionstyle=f"arc3,rad={rad}", ls=(0, (4, 3)) if dashed else "-", zorder=1.5, shrinkA=2, shrinkB=2))
    if label:
        mx, my = (p[0] + q[0]) / 2 + loff[0], (p[1] + q[1]) / 2 + loff[1]
        ax.text(mx, my, label, ha=ha, va="bottom", fontsize=fs, color=MUTED, style="italic", zorder=4,
                bbox=dict(fc="white", ec="none", pad=0.6, alpha=0.9))


def header(ax, w, h, title, sub):
    ax.text(0.4, h - 0.35, title, ha="left", va="top", fontsize=17, weight="bold", color=INK)
    ax.text(0.4, h - 0.85, sub, ha="left", va="top", fontsize=11, color=MUTED)


def footer(ax, text):
    ax.text(0.4, 0.22, text, ha="left", va="bottom", fontsize=8.6, color=MUTED, style="italic")


def save(fig, name):
    for ext in ("png",):
        fig.savefig(OUT / f"{NAMES.get(name, name)}.{ext}", dpi=200 if ext == "png" else None, facecolor="white")
    plt.close(fig)


# ---------------------------------------------------------------- Wunder: pipeline
def wunder_pipeline():
    W, H = 17.2, 6.9
    fig, ax = canvas(W, H)
    header(ax, W, H, "Alfa Connectome (Wunder Fund): end-to-end pipeline",
           "94th of 5,841 teams (top 1.6%)  ·  public score 0.5935 (organisers' baseline) → 0.66230  ·  Oct 7 – Nov 10, 2026")
    stages = [
        ("1. Data & metric study", "data", [
            "212M training rows,", "  112 columns each", "10,607 train / 1,873", "  validation sequences",
            "Baseline overshot the", "  metric's ±2 clip: ×0.25", "  = 83% of my total gain",
            "Targets decoded: 4 step", "  sizes, next-swing extreme", "Validation is a different", "  period (AUC 0.975)"]),
        ("2. Training (GPU queues)", "train", [
            "Teacher ensemble →", "  distilled minGRU students", "The metric itself as the", "  loss (direction × size)",
            "Aux targets: near-term", "  book events", "Stateful 2,000-row", "  windows + weight EMA",
            "Volume / price", "  augmentation", "~370 training runs"]),
        ("3. Validation", "eval", [
            "Validation sequences", "200 held-out train", "  sequences", "ESf: fold-honest stack",
            "  on held-out train", "Two seeds per verdict", "Failure analysis before", "  closing an idea",
            "ESf predicted +0.00042;", "  the board gave +0.00050"]),
        ("4. Members & stack", "train", [
            "\"F\" versions retrained", "  on train + validation", "5 members: W384F,", "  mktsoupF, mixF, wpg,",
            "  organisers' baseline", "Ridge stack, weights", "  fitted on train-only", "  twins",
            "Error correlation 0.97–", "  0.99 (mine), 0.95–0.96", "  (baseline)"]),
        ("5. Inference engine", "eng", [
            "ONNX → Numba → C", "AVX-512 VNNI kernels,", "  self-tested at load", "One fused call per row;",
            "  changed inputs only", "int8 weights; 4-bit on", "  the widest member", "Table-based activations",
            "Bit-exact / metric-equal", "  check on every step"]),
        ("6. Package & submit", "ship", [
            "20 MB zip", "39.4M rows on one CPU", "  core in 60 minutes", "Guard: switch to a", "  cheaper package late",
            "  in the run", "Checked offline before", "  every upload", "sub48: 56 min, 0.66230",
            "94th / 5,841 private", "  (up 108 places)"]),
    ]
    x0, bw, gap, by, bh = 0.4, 2.48, 0.38, 1.7, 3.75
    for i, (t, k, b) in enumerate(stages):
        x = x0 + i * (bw + gap)
        box(ax, x, by, bw, bh, t, b, k, fs=9.1, tfs=10.6)
        if i:
            arrow(ax, (x - gap, by + bh / 2), (x, by + bh / 2))
    # feedback: validation verdicts choose the next training run
    xa = x0 + 1 * (bw + gap) + bw / 2; xb = x0 + 2 * (bw + gap) + bw / 2
    arrow(ax, (xb, by), (xa, by), None, rad=-0.45, dashed=True)
    ax.text((xa + xb) / 2, by - 0.8, "verdicts pick the next run", ha="center", va="top", fontsize=8.6, color=MUTED, style="italic")
    footer(ax, "Claude (Anthropic, via Claude Code) wrote most of the code and ran the training queues and analyses; "
               "the direction, the project's rules, every upload and the final selection were mine.")
    save(fig, "wunder_pipeline")


# ---------------------------------------------------------------- Wunder: architecture
def wunder_architecture():
    W, H = 16.0, 8.6
    fig, ax = canvas(W, H)
    header(ax, W, H, "Alfa Connectome (Wunder Fund): model and runtime architecture",
           "A causal, streaming ensemble: one row in, two predictions out, state reset between sequences")
    # Panel A: one member
    ax.text(0.4, 7.25, "A. One member (minGRU)", fontsize=12.5, weight="bold", color=INK)
    ax_x, aw = 0.6, 4.9
    box(ax, ax_x, 6.35, aw, 0.6, None, ["Row t: 112 inputs + one-row differences"], "data", align="center", fs=9.6)
    box(ax, ax_x, 5.5, aw, 0.55, None, ["Input projection → width H (128–384)"], "gray", align="center", fs=9.6)
    box(ax, ax_x, 2.35, aw, 2.85, "minGRU block  × L (3–4 layers)", [], "train", tfs=10.5)
    inner = [("LayerNorm → Linear(H → 2H): gate z, candidate c", 4.45),
             ("h_t = (1 − z) · h_{t−1} + z · g(c),  + residual", 3.75),
             ("GELU MLP (H → 2H → H),  + residual", 3.05)]
    for s, yy in inner:
        box(ax, ax_x + 0.25, yy - 0.2, aw - 0.5, 0.5, None, [s], "gray", align="center", fs=9.1, z=3)
    for a_, b_ in ((4.25, 4.05), (3.55, 3.35)):
        arrow(ax, (ax_x + aw / 2, a_), (ax_x + aw / 2, b_))
    arrow(ax, (ax_x + aw / 2, 6.35), (ax_x + aw / 2, 6.05))
    arrow(ax, (ax_x + aw / 2, 5.5), (ax_x + aw / 2, 5.2))
    # recurrent state loop
    arrow(ax, (ax_x + aw, 3.2), (ax_x + aw, 4.3), rad=0.9, color="#2e8a4f", lw=1.8)
    ax.text(ax_x + aw + 0.62, 3.75, "state h\ncarried\nrow to row", fontsize=8.4, color="#2e8a4f", va="center", style="italic")
    box(ax, ax_x, 1.25, aw, 0.75, None, ["Output head: direction × size", "→ t0, t1"], "eng", align="center", fs=9.6, lh=0.27)
    arrow(ax, (ax_x + aw / 2, 2.35), (ax_x + aw / 2, 2.0))
    box(ax, ax_x + 0.4, 0.55, aw - 0.8, 0.45, None, ["Aux heads (training only): near-term book events"], "gray", dashed=True, align="center", fs=8.4)
    # Panel B: stack
    bx = 7.0
    ax.text(bx - 0.2, 7.25, "B. Final ensemble (sub48)", fontsize=12.5, weight="bold", color=INK)
    members = [("W384F", "minGRU 384 × 4, 4-bit MLP weights", "train"),
               ("mktsoupF", "192 × 3, distilled from step-size specialists", "train"),
               ("mixF", "128 × 3, mixture output head", "train"),
               ("wpg", "128 × 3, pooled-Pearson loss", "train"),
               ("baseline", "organisers' GRU, ported to my engine", "gray")]
    mw, mh = 3.6, 0.82
    ys = [6.05 - i * 1.0 for i in range(5)]
    for (n, d, k), yy in zip(members, ys):
        box(ax, bx, yy, mw, mh, None, [n, d], k, align="center", fs=9.0, lh=0.28)
        ax.texts[-2].set_weight("bold")
    sx, sy, sw, sh = bx + mw + 0.6, 3.55, 1.4, 1.3
    box(ax, sx, sy, sw, sh, None, ["Ridge", "stack", "(t0, t1)"], "eval", align="center", fs=10, lh=0.33)
    for yy in ys:
        arrow(ax, (bx + mw, yy + mh / 2), (sx, sy + sh / 2), lw=1.1)
    ax.text(bx, 0.75, "Every member is causal and streaming. Stack weights were fitted on each member's\n"
                      "train-only twin, because the \"F\" members had also trained on validation.",
            fontsize=8.6, color=MUTED, style="italic", va="bottom")
    # Panel C: runtime
    cx, cw = 13.35, 2.3
    ax.text(cx - 0.05, 7.25, "C. Runtime", fontsize=12.5, weight="bold", color=INK)
    box(ax, cx, 2.15, cw, 4.75, "One CPU core", [
        "C engine, AVX-512", "  VNNI kernels", "Self-tested at load", "int8 weights; 4-bit", "  on W384F",
        "Recompute only", "  changed inputs", "Table-based", "  activations", "Guard wrapper drops", "  to a cheaper package",
        "  if the clock runs long", "39.4M rows in 56 min", "  (≈ 85 µs per row)", "20 MB package"], "eng", fs=9.0, tfs=10.5)
    arrow(ax, (sx + sw, sy + sh / 2), (cx, sy + sh / 2), "compiled\ninto", loff=(0, 0.75), fs=8.2)
    ax.text(0.6, 0.12, "Training runs each 2,000-row window as one parallel log-space scan; at test time each row is one step with no "
                       "hidden-state matrix product.", fontsize=8.6, color=MUTED, style="italic")
    save(fig, "wunder_architecture")


# ---------------------------------------------------------------- SIG: system architecture
def sig_architecture():
    W, H = 16.0, 8.9
    fig, ax = canvas(W, H)
    header(ax, W, H, "SIG Predictions Cup (2026 midterms): system architecture",
           "An automated trading service for 237 binary election markets, Oct 1 – Nov 4, 2026")
    # external
    ax.text(0.4, 7.45, "External venues & data", fontsize=12, weight="bold", color=INK)
    box(ax, 0.4, 6.05, 3.1, 1.15, "Polymarket", ["Price for every race,", "  recorded each minute"], "ext", fs=9, tfs=10.5)
    box(ax, 0.4, 4.7, 3.1, 1.15, "Kalshi", ["Independent check: median", "  0.9c gap on 68 races"], "ext", fs=9, tfs=10.5)
    box(ax, 0.4, 1.1, 3.1, 3.3, "SIG venue (API)", ["237 binary markets", "YES + NO pay exactly 1", "Peer-to-peer matching,",
                                                    "  mostly 1-tick spreads", "REST orders + websocket", "  books", "30 writes / minute"], "ext", fs=9, tfs=10.5)
    # droplet container
    dx, dy, dw, dh = 4.15, 0.85, 7.05, 6.85
    box(ax, dx, dy, dw, dh, None, [], "cont", lw=1.2, z=1)
    ax.text(dx + 0.2, dy + dh - 0.15, "DigitalOcean server (1 GB) · systemd units", fontsize=11, weight="bold", color=INK, va="top")
    lx, rx, cw = dx + 0.25, dx + 3.65, 3.15
    feeds = box(ax, lx, 5.35, cw, 1.75, "Price feed", ["SIG books (websocket)", "Benchmarks each minute", "→ SQLite"], "data", fs=9, tfs=10.5)
    box(ax, rx, 5.35, cw, 1.75, "Supervisor + heartbeat", ["Monitor-only:", "  alerts, never trades", "Heartbeat checks"], "gray", fs=9, tfs=10.5)
    box(ax, lx, 3.05, dw - 0.5, 1.95, "Trader (Python asyncio)", [], "train", fs=9, tfs=10.5)
    ax.text(lx + 0.18, 4.5, "• Core book: buy SIG favourites priced\n   ≥ 5c below Polymarket, hold to settlement\n• Capital rotation into wider gaps",
            fontsize=9, color=INK, va="top", linespacing=1.35)
    ax.text(rx + 0.12, 4.5, "• Isolated pilots: fresh-gap, parity,\n   fair-band maker\n• Election-night trigger (95% + Kalshi)",
            fontsize=9, color=INK, va="top", linespacing=1.35)
    box(ax, lx, 0.98, cw, 1.85, "Safety core", ["Idempotent orders, reconciled", "  before any retry", "Rate-limit gate",
                                                 "Dead-man switch", "Per-feature flags"], "eng", fs=8.9, tfs=10.5, lh=0.245)
    box(ax, rx, 0.98, cw, 1.85, "Pilot isolation", ["Own cash; market lease", "Loss limit; STOP file", "Write cap; venue-side",
                                                    "  order expiry"], "eng", fs=8.9, tfs=10.5, lh=0.245)
    arrow(ax, (lx + cw / 2, 5.35), (lx + cw / 2, 5.0), "reads", loff=(0.35, -0.12), ha="left")
    arrow(ax, (rx + cw / 2, 5.35), (rx + cw / 2, 5.0), "watches", dashed=True, loff=(0.35, -0.12), ha="left")
    arrow(ax, (lx + cw / 2, 3.05), (lx + cw / 2, 2.83))
    arrow(ax, (rx + cw / 2, 3.05), (rx + cw / 2, 2.83))
    arrow(ax, (rx, 1.85), (lx + cw, 1.85))
    # venue links
    arrow(ax, (lx, 1.85), (3.5, 1.85), "orders", loff=(0, 0.06))
    arrow(ax, (3.5, 3.55), (lx, 3.55), "fills, positions", loff=(0, 0.06))
    arrow(ax, (3.5, 4.2), (lx, 5.6), "books", loff=(-0.05, 0.02))
    arrow(ax, (3.5, 6.6), (lx, 6.6))
    arrow(ax, (3.5, 5.3), (lx, 6.1))
    # local
    ox, ow = 11.85, 3.75
    ax.text(ox, 7.45, "Local machine", fontsize=12, weight="bold", color=INK)
    box(ax, ox, 4.75, ow, 2.45, "Research", ["Recorded data, snapshot", "  off the server (1 GB RAM)", "822k market-minutes of",
                                              "  SIG vs Polymarket", "Settlement-outcome tests", "  with bootstrap intervals"], "eval", fs=9, tfs=10.5)
    box(ax, ox, 1.05, ow, 3.35, "Tests & launch gate", ["~1,300 tests by the end", "Chaos + isolation stress:", "  50 seeds × 400 ticks,",
                                                        "  zero violations", "Mutation tests; dry run", "  against the real venue",
                                                        "Shadow → validate →", "  break-test → pilot → scale"], "ship", fs=9, tfs=10.5)
    arrow(ax, (dx + dw, 6.3), (ox, 6.3), "SQLite snapshots", loff=(0, 0.05))
    arrow(ax, (ox, 2.6), (dx + dw, 2.6), "deploy", loff=(0, 0.05))
    footer(ax, "Claude (Anthropic, via Claude Code) wrote most of the code, ran the research and deployed to the server; "
               "strategy direction, every go-live and every capital limit were mine.")
    save(fig, "sig_architecture")


# ---------------------------------------------------------------- SIG: pipelines
def sig_pipeline():
    W, H = 17.2, 9.4
    fig, ax = canvas(W, H)
    header(ax, W, H, "SIG Predictions Cup (2026 midterms): trading and launch pipelines",
           "+$8,849 on a 100,000 start, rank 557 of 5,141 participants, top 11% (provisional until House, Senate and MN-01 settle)")
    bw, gap, bh = 2.45, 0.35, 1.55

    def lane(y, label, items, notes=None):
        ax.text(0.4, y + bh + 0.18, label, fontsize=12, weight="bold", color=INK)
        for i, (t, b, k) in enumerate(items):
            x = 0.4 + i * (bw + gap)
            box(ax, x, y, bw, bh, t, b, k, fs=8.9, tfs=10.2, lh=0.24)
            if i:
                arrow(ax, (x - gap, y + bh / 2), (x, y + bh / 2))
        for i, n in (notes or {}).items():
            x = 0.4 + i * (bw + gap)
            ax.text(x + 0.05, y - 0.08, n, fontsize=8.3, color=MUTED, style="italic", va="top", linespacing=1.3)

    lane(6.25, "Core book: how a trade happened", [
        ("Prices", ["SIG book (websocket)", "Polymarket each minute", "Kalshi as a cross-check"], "data"),
        ("Signal", ["Favourite ≥ 5c cheaper on", "  SIG than on Polymarket", "  (favourite-longshot bias)"], "eval"),
        ("Sizing", ["5% per-market cap", "  with trims", "Rotation if a gap is", "  ≥ 3c wider"], "train"),
        ("Order", ["Through the safety core:", "  idempotent, rate-gated,", "  reconciled on retry"], "eng"),
        ("Hold to settlement", ["Marked at SIG's biased", "  price until then", "  (−$4,644 at close)"], "gray"),
        ("Settled", ["+10.9c per share on", "  77 markets (90% CI", "  +6.5c to +15.0c)"], "ship"),
    ])
    lane(3.85, "After launch week: how a strategy reached capital", [
        ("Shadow", ["Decisions logged,", "  no orders"], "gray"),
        ("Validate", ["Replay on recorded data", "  with honest costs"], "eval"),
        ("Break-test", ["One failing test per", "  attack, written first", "Chaos + isolation stress"], "ship"),
        ("Small pilot", ["Own cash, market lease,", "  loss limit, STOP file,", "  write cap"], "eng"),
        ("Scale", ["Only after the pilot", "  behaved on the venue"], "train"),
    ], notes={1: "Dropped here: two-sided market\nmaking, mean reversion, a 23-feature\nscan, book pressure, Polymarket lag",
              2: "Caught 4 critical bugs in the\nfirst pilot (double buy, oversell,\nlease lapse, stale-price crash)",
              3: "Fresh-gap +\\$308 · parity +\\$11.54\nfair-band maker −\\$32"})
    lane(0.95, "Election night (Nov 3), built from a 2024 replay", [
        ("Polymarket ≥ 95%", ["No 2024 losing side", "  ever traded at 95%"], "data"),
        ("Kalshi agrees", ["Second real-money", "  venue confirms"], "data"),
        ("SIG ask ≥ 1c lower", ["The SIG price still", "  lags the call"], "eval"),
        ("Buy $2k per race", ["$40k budget, raised by", "  an automated sell-down", "  ($40,263 in 21 min)"], "eng"),
        ("Hold to settlement", ["22 races filled at", "  ~93c average"], "gray"),
        ("Result", ["21 of 21 settled races", "  paid: +$2,869", "  (Senate leg pending)"], "ship"),
    ])
    footer(ax, "Claude (Anthropic, via Claude Code) built and replayed the research and wrote most of the code; "
               "the strategy choices, launch rules and capital limits were mine.")
    save(fig, "sig_pipeline")


if __name__ == "__main__":
    sig_architecture(); sig_pipeline()
    print("ok")
