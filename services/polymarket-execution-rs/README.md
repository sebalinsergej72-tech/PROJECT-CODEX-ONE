# Polymarket Execution Sidecar (Rust)

Low-latency Rust sidecar for the Polymarket smart copy bot.

Current scope:
- `/health`
- `/v1/market/snapshot/:token_id`
- `/v1/market/prime`
- `/v1/market/register_hot`
- `/v1/market/executable/:token_id`
- `/v1/market/executable/prime`
- `/v1/orders/open`
- `/v1/orders/:order_id/status`
- `/v1/fills/account`
- `/v1/signals/hot_scan`
- `/v1/execute/plan`
- `/v1/fills/reconcile`

Local push IPC:
- TCP stream on `127.0.0.1:$EXECUTION_SIDECAR_PUSH_PORT`
- line-delimited JSON
- Python subscribes with `{"type":"subscribe","token_ids":[...]}`
- Rust pushes `{"type":"snapshot","token_id":"...","snapshot":{...}}`

Hot market registry:
- token IDs can be registered with TTL and priority
- push loop only refreshes registered hot tokens

Hot wallet ingest:
- `/v1/signals/hot_scan` batch-loads recent wallet trades from the Polymarket data API
- response is already normalized into wallet signal rows for Python `TradeMonitor`

Current executable snapshot behavior:
- returns a normalized executable market view
- precomputes buy/sell VWAP for `$5`, `$10`, and `$25`
- reports top-5 bid/ask liquidity, `last_update_ts`, and `snapshot_age_ms`

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

Authenticated read behavior:
- `/v1/orders/open` uses Polymarket Level 2 auth headers and paginates open orders
- `/v1/orders/:order_id/status` fetches one authenticated order payload
- `/v1/fills/account` paginates authenticated trade history after a given timestamp
- these endpoints are designed as a faster fallback path for Python lifecycle monitoring

The sidecar is intentionally introduced as an optional execution-plane service.
Python remains the control plane and fallback path.

## Run

```bash
cd services/polymarket-execution-rs
cargo run
```

Environment variables:
- `EXECUTION_SIDECAR_PORT` default `8787`
- `EXECUTION_SIDECAR_PUSH_PORT` default `8788`
- `EXECUTION_SIDECAR_PUSH_INTERVAL_MS` default `150`
- `POLYMARKET_DATA_API_HOST` default `https://data-api.polymarket.com`
- `POLYMARKET_HOST` default `https://clob.polymarket.com`
- `POLYMARKET_PRIVATE_KEY` used to derive the signer address for Level 2 reads
- `POLYMARKET_SIGNER_ADDRESS` optional explicit signer address override
- `POLYMARKET_API_KEY`
- `POLYMARKET_API_SECRET`
- `POLYMARKET_API_PASSPHRASE`
- `EXECUTION_SIDECAR_CACHE_TTL_SECONDS` default `10`
- `RUST_LOG` default `info`
