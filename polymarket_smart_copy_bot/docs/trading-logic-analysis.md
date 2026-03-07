# Trading Logic Analysis

Last updated: 2026-03-07
Source of truth: `origin/main` at commit `4c74c74`
Live runtime referenced in this document: `https://project-codex-one-production.up.railway.app`

## 1. Scope

This document describes the current trading logic after the latest execution-safety, scoring, and risk-control updates.

It separates:

- selection logic: how wallets qualify and how they are ranked
- signal logic: how selected wallets produce copy-trade intents
- risk logic: how intents are accepted or resized
- execution logic: how accepted intents become Polymarket orders
- live runtime observations: what production is doing right now

This file is intentionally detailed and should be treated as the working reference for strategy review.

## 2. Protected Strategy Surface

These files contain the most sensitive trading logic:

- `core/wallet_discovery.py`
- `core/trade_monitor.py`
- `core/risk_manager.py`
- strategic orchestration in `core/background_tasks.py`

These files are execution support and materially affect realized PnL, but they are not the wallet-selection thesis itself:

- `core/trade_executor.py`
- `core/portfolio_tracker.py`
- `data/polymarket_client.py`

## 3. End-to-End Pipeline

The bot runs through this flow:

1. startup / restore
   - restore positions from DB
   - sync account positions from Polymarket
   - load seed wallets
   - run discovery
   - recalc capital

2. discovery
   - fetch leaderboard candidates
   - enrich with activity and trade history
   - apply hard filters
   - score survivors
   - persist top wallets as `qualified_wallets`

3. trade monitoring
   - load enabled qualified wallets
   - fetch latest source-wallet trades
   - dedupe by `external_trade_id`
   - shape copy-trade intents
   - build reconcile-close intents if source positions disappear

4. risk evaluation
   - block by drawdown / price filter / exposure caps
   - size trade via proportional copy + light Kelly
   - reject dust trades

5. execution
   - pre-check position caps
   - pre-check liquidity and price movement
   - submit order to Polymarket
   - reconcile fills / cancels / expirations
   - sync positions again

## 4. Runtime Jobs and Orchestration

The orchestrator in `core/background_tasks.py` controls these recurring jobs:

- `wallet_score_refresh`
- `trade_monitor`
- `portfolio_refresh`
- `order_fill_monitor`
- `stale_order_cleanup`
- `capital_recalc`

Important runtime behaviors:

- discovery and execution are decoupled
- the bot can hold open positions while no new trades are executed
- capital is path-dependent within a cycle because exposure and cash are refreshed as trades progress
- before each trade-monitor cycle in live mode, account positions are synced from the exchange

That pre-cycle sync is now an explicit part of the runtime safety model.

## 5. Wallet Discovery Architecture

`core/wallet_discovery.py` is still the center of the wallet-selection logic.

### 5.1 Candidate sourcing

Candidates are pulled from Polymarket leaderboard-style endpoints across:

- `OVERALL`
- `POLITICS`
- `SPORTS`
- `CRYPTO`

Each seed can carry hints such as:

- `address`
- `name`
- `niches`
- `monthly_pnl_pct`
- `win_rate_hint`
- `trades_90d_hint`
- `trades_30d_hint`
- `profit_factor_hint`
- `avg_size_hint`
- `last_trade_ts_hint`
- `consecutive_losses_hint`
- `wallet_age_days_hint`
- `volume_hint`
- `leaderboard_rank_hint`

### 5.2 Qualification thresholds

Aggressive mode defaults:

- `min_trades_90d = 120`
- `min_win_rate = 0.65`
- `min_profit_factor = 1.55`
- `min_avg_size = 350`
- `max_days_since_last_trade = 7`
- `max_consecutive_losses = 5`
- `min_wallet_age_days = 30`

Conservative mode defaults:

- `min_trades_90d = 160`
- `min_win_rate = 0.69`
- `min_profit_factor = 1.75`
- `min_avg_size = 550`
- `max_days_since_last_trade = 5`
- `max_consecutive_losses = 4`
- `min_wallet_age_days = 45`

Discovery remains strict. Most candidates still die before ranking matters.

### 5.3 Hard filters

For each candidate, the bot computes or infers:

- `trades_90d_count`
- `win_rate`
- `profit_factor`
- `avg_size`
- `days_since_last`
- `consecutive_losses`
- `wallet_age_days`
- `pnl_consistency`

Qualification chain:

1. minimum 90-day trade count
2. minimum win rate
3. minimum profit factor
4. minimum average size
5. recency gate
6. consecutive-loss gate
7. wallet-age gate
8. anti-lottery consistency check

Important anti-lottery rule:

- if median PnL is negative but mean PnL is positive over a sufficient sample, the wallet is rejected as lottery-style behavior

### 5.4 Heuristic fallbacks

When Polymarket does not provide full quality metrics directly, the bot estimates:

- `monthly_pnl_pct` from `pnl / volume`
- `win_rate_hint` from positive `pnl / vol`
- `profit_factor_hint` from `(vol + pnl) / (vol - pnl)`
- `wallet_age_days` from leaderboard maturity signals

These heuristics remain useful, but they reduce metric fidelity. Discovery is therefore best understood as "strict filtering with partially inferred quality metrics".

## 6. Updated Score Formula

After the recent changes, score is now:

```text
size_component = min(avg_size / 100, 50)

score =
    win_rate * 120
  + monthly_pnl_pct * 80
  + trades_30d_count * 0.6
  + size_component
  + profit_factor * 25
  - days_since_last * 3
  - consecutive_losses * 15

score = max(score - tradability_penalty, 0)
```

### 6.1 What changed

Two important changes now materially affect ranking:

1. `avg_size` is capped
   - old behavior: `avg_size / 100` could dominate the score
   - new behavior: size contributes at most `50` points

2. tradability penalty is applied
   - based on the bot's own 14-day execution history for that wallet
   - low fill-rate wallets are automatically pushed down

### 6.2 Tradability penalty table

If a wallet has at least `5` recent attempts:

- fill rate `< 0.15` => `-100`
- fill rate `< 0.30` => `-60`
- fill rate `< 0.50` => `-30`
- fill rate `< 0.70` => `-10`
- otherwise `0`

This is the most important new bridge between theory and live tradability.

### 6.3 Strategic effect of the update

Before this update, ranking behaved like:

- hard quality gate
- then sort mostly by wallet size

After this update, ranking is closer to:

- hard quality gate
- then sort by a mix of quality, recent activity, and actual live-copyability

That is a meaningful strategic change.

## 7. Final Selection

Final selection still works as:

- sort descending by score
- take the top enabled limit

Current aggressive wallet cap is `15`, but production currently has only `12` wallets surviving the full pipeline.

There is still no diversification constraint in final selection:

- no category caps
- no behavior-profile caps
- no concentration caps

That remains a future strategy lever, not a current one.

## 8. Trade Monitoring Logic

`core/trade_monitor.py` converts selected wallets into copyable intents.

### 8.1 Wallet scan

For each enabled wallet:

- fetch recent wallet trades
- ignore already-seen `external_trade_id`
- enforce short-term toggle
- enforce market blacklist
- enforce optional price filter

### 8.2 New market blacklist

The bot now blocks clearly problematic short-term markets earlier in the pipeline.

Blacklisted slug patterns:

- `.*-1h$`
- `.*-1d$`
- `.*hourly.*`
- `.*minute.*`

Blacklisted category:

- `short_term_crypto`

This is a signal-layer filter, not an execution-layer error.

### 8.3 Intent shaping

Each intent now carries:

- wallet quality fields
- market metadata
- `market_slug`
- `market_category`
- source side, price, size
- short-term flag

### 8.4 Real-time wallet throttling

New per-cycle throttling exists inside the monitor loop:

- each wallet tracks consecutive failures within the current cycle
- after `5` consecutive failures, that wallet is skipped for the remainder of the cycle
- success resets the wallet's in-cycle failure counter

This is faster-reacting than the 14-day tradability penalty.

### 8.5 Reconcile-close logic

Monitor still compares:

- source wallet open positions
- local mirrored positions

If source no longer holds a position, the bot can emit a synthetic close intent.

This remains important for keeping the copy-book aligned with source behavior.

## 9. Updated Risk Logic

`core/risk_manager.py` now reflects a more conservative aggressive mode.

### 9.1 Global gates

Before sizing:

- global drawdown stop can block all trading
- daily drawdown stop can block all trading
- optional price filter can block by entry price range

### 9.2 Aggressive mode sizing

Core caps:

- `per_wallet_cap = capital * 0.15`
- `per_position_cap = capital * 0.10`
- `total_exposure_cap = capital * 0.65`

Updated wallet multiplier:

```text
wallet_multiplier = 0.8 + min(max(wallet_score, 0.0), 1.5) * 0.6
wallet_multiplier = min(wallet_multiplier, 2.0)
```

High-conviction boost still exists:

- if source trade size is more than 2x wallet average trade size and boost is enabled, multiply by `high_conviction_multiplier`
- final multiplier is still capped by `2.0`

Updated light Kelly:

```text
p = clipped win_rate
q = 1 - p
b = clipped profit_factor, max 2.5

kelly = max(((b * p) - q) / b, 0.0)
light_kelly_fraction = min(kelly * 0.20 * kelly_multiplier, 0.15)
```

Raw target size:

```text
raw_target_size =
    source_size_usd * wallet_multiplier
  + capital * kelly_fraction
```

Final target size is capped by:

- per-position cap
- remaining wallet capacity
- remaining total capacity
- available cash

New minimum trade size:

- if final size `< $3.00`, trade is rejected with `size_below_minimum`

### 9.3 Conservative mode sizing

Conservative mode remains simple:

- target is `source_size_usd * 0.15`
- lower caps
- no Kelly
- still respects `$3` minimum size

### 9.4 Strategic meaning of the update

Compared with the earlier live version, the bot is now:

- less aggressive in Kelly sizing
- less aggressive in wallet multiplier inflation
- less willing to open dust positions

This should reduce variance and log noise, at the cost of smaller gross exposure.

## 10. Updated Execution Logic

`core/trade_executor.py` is where the latest safety package had the biggest effect.

### 10.1 New pre-execution safety checks

Before placing an order, the bot now explicitly checks:

1. `SELL` without confirmed local position
   - skip with `sell_without_confirmed_position`

2. market position cap pre-check
   - resize or reject before order placement

3. minimum residual size
   - reject dust after resizing

4. orderbook tradability
   - skip if no orderbook
   - skip if insufficient top-of-book liquidity

5. price deviation
   - compare source price to current best bid/ask
   - skip if moved by more than `3%`

### 10.2 Liquidity pre-check

The bot now loads the orderbook before sending the order and computes:

- top-5 ask notional for `BUY`
- top-5 bid notional for `SELL`

Required liquidity:

```text
required = max(target_size_usd * 1.5, 15)
```

If available liquidity is below that threshold, the trade is skipped early.

This is intended to prevent wasting order attempts on effectively empty markets.

### 10.3 Inventory-aware sells

For `SELL`, the bot now:

- requires confirmed local position
- trims sell size to what is actually available
- rejects if the residual is too small

This is important after manual closes or source/local desync.

### 10.4 Price movement guard

The bot compares source execution price to the current best price:

- `BUY` uses current best ask
- `SELL` uses current best bid

If deviation exceeds `3%`, the trade is skipped.

This is the meaning of reasons like:

- `price_moved:45.6%`

That skip is intentional protection against late, degraded fills.

### 10.5 Order types and lifecycle

Current behavior is still:

- `BUY` in aggressive flow uses `GTC` with fill monitoring and one controlled reprice path
- statuses can become `submitted`, `partial`, `filled`, `canceled`, `expired`, `skipped`, `failed`

Stale-order cleanup and fill reconciliation remain part of the lifecycle.

## 11. Polymarket Client Changes

`data/polymarket_client.py` now carries part of the runtime safety model.

### 11.1 Price validation

Valid price range:

- minimum valid price: `0.01`
- maximum valid price: `0.99`

Invalid prices return `invalid_price` and are skipped.

This is designed to avoid pathological low-price and near-1.00 markets that are hard to size safely.

### 11.2 Orderbook snapshot support

The client now exposes a parsed orderbook snapshot:

- `bids`
- `asks`
- `best_bid`
- `best_ask`

This is what makes the new liquidity and price-movement guards possible.

## 12. Position Sync Model

Production now syncs exchange positions at two important moments:

1. regular portfolio refresh
2. explicitly before each trade-monitor cycle in live mode

Practical consequence:

- manual closes outside the bot are less likely to leave stale mirrored state
- sell-side intent validation is now stricter because the bot checks confirmed inventory more often

This does not guarantee perfect sync in all edge cases, but it is materially safer than the older runtime.

## 13. Current Live Snapshot

Current production snapshot referenced here:

- `risk_mode = aggressive`
- `tracked_wallets = 12`
- `discovery_scanned_candidates = 151`
- `discovery_passed_filters = 12`
- `seed_wallets.enabled = 12`
- `seed_wallets.avg_score = 202.7075`
- `total_equity_usd = 63.8685`
- `exposure_usd = 14.1583`
- `available_cash_usd = 42.7604`
- `open_order_reserve_usd = 6.9498`
- `open_positions = 8`
- `daily_pnl_usd = -6.6092`
- `cumulative_pnl_usd = -4.8512`

Interpretation:

- the new discovery logic is live
- selection is stricter than before the recent update
- score inflation from oversized `avg_size` is gone
- the bot is live and holds real open positions

## 14. Current Production Trade Tape

Fresh post-deploy trades show the new guards in action.

Recent examples:

- `2026-03-07 02:51:44 UTC`
  - wallet `0x05e26c...6e12`
  - `BUY`
  - status `skipped`
  - reason `price_moved:45.6%`

- `2026-03-07 02:45:47 UTC`
  - wallet `0x05e26c...6e12`
  - `SELL`
  - status `skipped`
  - reason `sell_without_confirmed_position`

These are useful examples because they prove:

- new guards are active in production
- the bot is still seeing live source-wallet signals
- the bot is deliberately refusing low-quality or invalid copy attempts

## 15. What Improved After the Latest Update

The recent P0/P1/P2 package materially changed the system in these ways.

### 15.1 Better selection quality

- `avg_size` no longer dominates ranking
- wallets with poor live fill-rate are penalized
- low-value dust trades are filtered out

### 15.2 Better signal hygiene

- blacklisted short-term market patterns are rejected earlier
- in-cycle throttling reduces repeated low-value attempts from failing wallets

### 15.3 Better execution hygiene

- sell attempts require actual inventory
- orderbook liquidity is checked before submission
- price movement is checked before submission
- invalid prices are blocked at the client level

### 15.4 Better runtime safety

- account positions are synced before each live trade cycle
- manual closes outside the bot are less likely to poison subsequent sells

## 16. What Still Limits Live Performance

The system is cleaner now, but several realities still limit realized PnL.

### 16.1 Execution still blocks many signals

Even with better guards, some signals remain hard to trade because:

- source wallets act in thin markets
- price moves too fast before the bot arrives
- some markets still lack usable orderbooks

### 16.2 Heuristic discovery metrics still matter

Discovery quality is improved, but not fully "ground truth" because:

- win rate and PF are still partly inferred from upstream data
- leaderboard hints remain part of wallet evaluation

### 16.3 Final selected set is still not diversified

The bot still picks the top scored wallets without category caps.

That means:

- concentration risk is still structurally possible
- a small number of similarly behaving wallets can dominate live flow

## 17. Current End-to-End Assessment

### 17.1 Wallet selection alone

Assessment: `7 / 10`

Why:

- strict qualification
- better ranking after `avg_size` cap
- tradability penalty now grounds ranking in live experience
- still partly heuristic and still not diversification-aware

### 17.2 Live realized trading

Assessment: `4.5 / 10`

Why:

- the bot is safer and cleaner than before
- it now rejects obviously bad copy attempts earlier
- but live PnL is still modest and execution frictions remain substantial

This is an improvement over the earlier state, but not yet "fully tuned".

## 18. High-Impact Future Levers

These are analysis points only, not approved changes.

1. diversification-aware final wallet selection
2. deeper tradability-aware ranking by category / market type
3. additional execution tuning once the new safety baseline stabilizes

Because the latest update already changed both ranking and execution safety, the next strategy changes should be evaluated against a fresh live window, not against stale historical tape.

## 19. Recommended Reading Order

If reviewing the current strategy from scratch, use this order:

1. `config/settings.py`
2. `core/wallet_discovery.py`
3. `core/trade_monitor.py`
4. `core/risk_manager.py`
5. `core/trade_executor.py`
6. `data/polymarket_client.py`
7. `core/background_tasks.py`

This order moves from strategy thesis to signal generation to risk to execution reality.

## 20. Bottom Line

The latest version of the bot is meaningfully different from the earlier live build.

What is now better:

- wallet ranking is less distorted by wallet size
- wallets that are poor to copy in practice are penalized
- dust trades are removed
- short-term toxic markets are filtered earlier
- execution safety is much stronger
- manual-close desync is handled more safely

What still needs observation, not immediate redesign:

- actual fill quality after the new guardrails
- concentration of flow among the currently selected wallets
- realized PnL over a fresh post-update sample

The system is now best described as:

- a stricter, more execution-aware copy-trading engine
- still dependent on live market tradability for realized edge
