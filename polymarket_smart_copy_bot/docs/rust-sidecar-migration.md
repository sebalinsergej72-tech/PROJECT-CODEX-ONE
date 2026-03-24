# Rust Sidecar Migration

This document describes the first implemented phase of the Python -> Rust migration for the Polymarket smart copy bot.

## Goal

Keep Python as the control plane and gradually move latency-sensitive logic into a dedicated Rust execution sidecar.

## Implemented in this phase

### Shared contract

Python now has explicit sidecar request/response models in:

- `contracts/execution_sidecar.py`

These models define:

- market snapshot requests and responses
- execution plan requests and responses

### Rust sidecar service

A new service exists at:

- `services/polymarket-execution-rs`

Current endpoints:

- `GET /health`
- `GET /v1/market/snapshot/:token_id`
- `POST /v1/market/prime`
- `POST /v1/execute/plan`

Current responsibilities:

- fetch orderbook snapshots from Polymarket
- cache snapshots in memory with a short TTL
- run hot-path tradability checks (`no_orderbook`, `low_liquidity`, `price_moved`)
- build aggressive execution plans using the same slippage rules as Python

### Python integration

`data/polymarket_client.py` now supports an optional sidecar path behind feature flags:

- `EXECUTION_SIDECAR_ENABLED`
- `EXECUTION_SIDECAR_BASE_URL`
- `EXECUTION_SIDECAR_TIMEOUT_SECONDS`
- `EXECUTION_SIDECAR_MARKET_DATA_ENABLED`
- `EXECUTION_SIDECAR_EXECUTION_PLAN_ENABLED`

When enabled:

- `fetch_orderbook()` tries the Rust sidecar before local REST fallback
- `prime_market_data()` can backfill the local Python snapshot cache from the Rust sidecar
- `TradeExecutor` can ask the Rust sidecar to evaluate tradability and build an execution plan before falling back to Python

## What is still intentionally left in Python

- wallet discovery
- live-pool selection
- risk sizing
- dashboard / API
- Telegram integration
- portfolio accounting
- actual live order submission

## Why this split

This lets us migrate the latency-critical path without rewriting the whole bot at once.

## Safe rollout strategy

1. Run the Rust sidecar locally with Python fallback still enabled.
2. Enable only market-data offload first.
3. Compare behavior against the Python path.
4. Move execution planning and later order submission only after parity checks.

## Next suggested implementation steps

1. Add fill-monitor endpoints into Rust.
2. Move hot-wallet polling into Rust.
3. Add shadow-mode comparison metrics between Python and Rust decisions.
4. Move actual signed order submission after parity checks.
