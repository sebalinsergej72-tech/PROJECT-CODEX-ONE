# Polymarket Execution Sidecar (Rust)

Low-latency Rust sidecar for the Polymarket smart copy bot.

Current scope:
- `/health`
- `/v1/market/snapshot/:token_id`
- `/v1/market/prime`
- `/v1/execute/plan`
- `/v1/fills/reconcile`

Current `/v1/execute/plan` behavior:
- validates `no_orderbook`
- validates `low_liquidity`
- validates `price_moved`
- calculates aggressive/conservative execution plan fields
- returns a normalized order payload for Python submit/fallback

Current `/v1/fills/reconcile` behavior:
- consumes normalized fill rows plus `order_open`
- computes `submitted / partial / filled / canceled`
- returns deltas for position upsert and lifecycle updates

The sidecar is intentionally introduced as an optional execution-plane service.
Python remains the control plane and fallback path.

## Run

```bash
cd services/polymarket-execution-rs
cargo run
```

Environment variables:
- `EXECUTION_SIDECAR_PORT` default `8787`
- `POLYMARKET_HOST` default `https://clob.polymarket.com`
- `EXECUTION_SIDECAR_CACHE_TTL_SECONDS` default `10`
- `RUST_LOG` default `info`
