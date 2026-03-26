# Rust Sidecar Migration

This document describes the currently implemented phases of the Python -> Rust migration for the Polymarket smart copy bot.

## Goal

Keep Python as the control plane and gradually move latency-sensitive logic into a dedicated Rust execution sidecar.

## Implemented in this phase

### Shared contract

Python now has explicit sidecar request/response models in:

- `contracts/execution_sidecar.py`

These models define:

- market snapshot requests and responses
- executable snapshot requests and responses
- execution plan requests and responses
- fill reconciliation requests and responses

### Rust sidecar service

A new service exists at:

- `services/polymarket-execution-rs`

Current endpoints:

- `GET /health`
- `GET /v1/market/snapshot/:token_id`
- `POST /v1/market/prime`
- `POST /v1/market/register_hot`
- `GET /v1/market/executable/:token_id`
- `POST /v1/market/executable/prime`
- `GET /v1/orders/open`
- `GET /v1/orders/:order_id/status`
- `GET /v1/fills/account`
- `POST /v1/signals/hot_scan`
- `POST /v1/execute/plan`
- `POST /v1/fills/reconcile`

Current local push IPC:

- Rust sidecar also exposes a local TCP push stream for executable snapshots
- Python `ExecutionSidecarClient` keeps a mirror cache of pushed snapshots
- HTTP remains as fallback when the stream is disconnected or not warmed yet

Current hot market / ingest additions:

- Rust sidecar keeps a hot market registry with TTL
- Python registers token IDs from fresh intents and executable snapshot fetches
- Rust can batch-fetch and normalize recent trades for hot wallets
- Python `TradeMonitor` can use Rust hot-wallet scan before falling back to per-wallet polling

Current responsibilities:

- fetch orderbook snapshots from Polymarket
- cache snapshots in memory with a short TTL
- build executable market snapshots with precomputed VWAPs (`$5 / $10 / $25`)
- push executable snapshots over local TCP to reduce HTTP round-trips in the hot path
- keep a TTL-based hot market registry so push refresh stays focused on high-value token IDs
- batch-poll and normalize hot-wallet trade signals from the data API
- fetch authenticated open orders, order status, and account fills using Rust-side Level 2 auth headers
- run hot-path tradability checks (`no_orderbook`, `low_liquidity`, `price_moved`)
- build aggressive execution plans using the same slippage rules as Python
- reconcile fill lifecycle state from normalized fill rows plus `order_open`

### Python integration

`data/polymarket_client.py` now supports an optional sidecar path behind feature flags:

- `EXECUTION_SIDECAR_ENABLED`
- `EXECUTION_SIDECAR_BASE_URL`
- `EXECUTION_SIDECAR_TIMEOUT_SECONDS`
- `EXECUTION_SIDECAR_PUSH_ENABLED`
- `EXECUTION_SIDECAR_PUSH_HOST`
- `EXECUTION_SIDECAR_PUSH_PORT`
- `EXECUTION_SIDECAR_PUSH_RECONNECT_SECONDS`
- `EXECUTION_SIDECAR_HOT_MARKET_REGISTRY_ENABLED`
- `EXECUTION_SIDECAR_HOT_MARKET_TTL_SECONDS`
- `EXECUTION_SIDECAR_HOT_SIGNAL_INGEST_ENABLED`
- `EXECUTION_SIDECAR_MARKET_DATA_ENABLED`
- `EXECUTION_SIDECAR_EXECUTABLE_SNAPSHOT_ENABLED`
- `EXECUTION_SIDECAR_EXECUTION_PLAN_ENABLED`
- `EXECUTION_SIDECAR_FILL_RECONCILE_ENABLED`
- `EXECUTION_SIDECAR_SHADOW_MODE_ENABLED`
- `EXECUTION_SIDECAR_AUTHENTICATED_READS_ENABLED`

When enabled:

- `fetch_orderbook()` tries the Rust sidecar before local REST fallback
- `prime_market_data()` can backfill the local Python snapshot cache from the Rust sidecar
- `fetch_executable_snapshot()` can fetch a normalized executable market view without asking Python to walk the raw book
- `prime_executable_market_data()` can warm executable snapshots for hot tokens
- `ExecutionSidecarClient` can subscribe to hot token IDs and keep a pushed executable snapshot mirror in memory
- `register_hot_markets()` can keep important token IDs in the Rust hot registry
- `scan_hot_wallet_trades_via_sidecar()` can batch-load normalized wallet signals for the hot lane
- `TradeExecutor` can ask the Rust sidecar to evaluate tradability and build an execution plan before falling back to Python
- `TradeExecutor` can ask the Rust sidecar to compute fill reconciliation status before falling back to Python
- `PolymarketClient` can ask the Rust sidecar for authenticated `open orders / order status / account fills` before falling back to `py_clob_client`
- `TradeMonitor` and `TradeExecutor` can emit decision-audit rows so Python/Rust parity is visible instead of implicit
- `WalletDiscovery` now scores wallets with richer copyability metrics, not just raw fill-rate

## What is still intentionally left in Python

- wallet discovery
- live-pool selection
- risk sizing
- dashboard / API
- Telegram integration
- portfolio accounting
- actual live order submission and signed order creation

## Why this split

This lets us migrate the latency-critical path without rewriting the whole bot at once.

## Safe rollout strategy

1. Run the Rust sidecar locally with Python fallback still enabled.
2. Enable only market-data offload first.
3. Compare behavior against the Python path.
4. Move execution planning and later order submission only after parity checks.

## Next suggested implementation steps

1. Extend Rust ingest from hot-wallet batch scan to longer-lived polling loops and signal push.
2. Add richer parity dashboards / API views on top of `execution_decision_audits`.
3. Move actual signed order submission only after full order-builder parity is implemented and canary-tested.
4. Add copyability-aware market policy to the Rust execution plane as well, not only discovery.
