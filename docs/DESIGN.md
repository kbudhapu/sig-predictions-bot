# sig-bot

A standalone trading bot for the **SIG Predictions Cup** (prediction-market exchange,
`thesuper.market`). Sibling of — and independent from — the `mbappe` Alpaca bot; nothing
here touches that live system.

## Design

The battle-tested **safety skeleton is ported** from the mbappe bot (broker-agnostic
pieces: idempotency core, three-layer dead-man/alerting, degradation ladder, SQLite state
store, reconcile-boot engine, systemd resilience, monitoring sidecar). Only the
**SIG-specific adapter layer is new** (broker client, ingestor, session calendar,
strategies, and the order-path 429/Retry-After handler + outbound write-gate that mbappe
lacked).

```
sig-bot/
├── infra/        ported mbappe safety skeleton (proven foundation)
├── sig/          NEW SIG-specific: broker adapter, ingestor, session calendar
├── strategies/   NEW SIG strategies (arb detector first)
├── config/       committed non-secret config + gitignored secrets
├── main_sig.py   entry point
└── deploy/       systemd unit(s)
```

## Safety

- Secrets live **only** in gitignored config (`config/secrets.local.yaml` / `.env`); no key
  is ever hardcoded, printed, or committed.
- Autonomous and multi-leg trading stay **OFF** until the four live safety-gate tests pass
  (places/idempotency/rate-limit-backoff/cancel verified on live SIG).
- Ambiguous order responses **reconcile before retrying** — never assume a fill or a failure.
