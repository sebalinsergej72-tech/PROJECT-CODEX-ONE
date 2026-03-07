# Trading Logic Analysis

Last updated: 2026-03-07
Source of truth: `origin/main` at commit `1c1e08c`
Live runtime referenced in this document: `https://project-codex-one-production.up.railway.app`

## 1. Scope

This document describes the protected trading logic of the Polymarket copy-trading bot as it exists in the current GitHub version. It separates:

- core strategy logic: which wallets are selected, how signals are accepted, how size is chosen
- execution support: how approved signals are turned into orders on Polymarket
- runtime observations: what is happening in production right now

This is intentionally more detailed than the README. It is meant to be a working analysis file for future review, debugging, and design decisions.

## 2. Protected Strategy Surface

These files contain the core trading logic and should be treated as the highest-sensitivity area:

- `core/wallet_discovery.py`
- `core/risk_manager.py`
- `core/trade_monitor.py`
- strategic parts of `core/background_tasks.py`

These files support execution and materially affect realized PnL, but are not the wallet-selection strategy itself:

- `core/trade_executor.py`
- `core/portfolio_tracker.py`
- `data/polymarket_client.py`

## 3. End-to-End Pipeline

The live flow is:

1. Bootstrap / restore:
   - restore open positions
   - import seed wallets from `config/wallets.yaml` if DB is empty
   - run wallet discovery
   - refresh portfolio
   - run capital recalc
   - run trade monitor
   - run stale-order cleanup

2. Discovery:
   - fetch leaderboard candidates from Polymarket across categories
   - enrich candidates with hints and trade history
   - hard-filter each candidate
   - score survivors
   - persist top `N` wallets into `qualified_wallets`

3. Monitoring:
   - load enabled qualified wallets ordered by score descending
   - fetch latest wallet trades
   - dedupe against already-seen `external_trade_id`
   - produce copy-trade intents
   - also produce reconcile-close intents when source positions disappear

4. Risk evaluation:
   - reject trades outside risk constraints
   - size the trade
   - enforce per-wallet, per-position, and total portfolio caps

5. Execution:
   - convert approved intent into order request
   - submit to Polymarket CLOB
   - reconcile fills / cancellations / expirations
   - update local positions and portfolio snapshots

## 4. Runtime Jobs and Orchestration

The orchestrator in `core/background_tasks.py` is the runtime controller.

Important recurring jobs:

- `wallet_score_refresh`
- `trade_monitor`
- `portfolio_refresh`
- `order_fill_monitor`
- `stale_order_cleanup`
- `capital_recalc`

Important behaviors:

- discovery and trading are decoupled; a wallet can be selected long before its signals arrive
- trade monitor exits early if `trading_enabled` is false
- portfolio is recalculated after each executed intent inside the scan loop, so sizing is path-dependent within a single cycle
- drawdown protection can stop the cycle and close positions before new trades are evaluated

## 5. Wallet Discovery Architecture

`core/wallet_discovery.py` is the heart of the wallet-selection strategy.

### 5.1 Candidate sourcing

Candidates are collected from Polymarket leaderboard-style endpoints across:

- `OVERALL`
- `POLITICS`
- `SPORTS`
- `CRYPTO`

Each candidate is represented as `CandidateSeed` with possible hints:

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

This matters because a large part of the current strategy depends on what Polymarket exposes directly versus what the bot must infer.

### 5.2 Qualification thresholds

Thresholds differ by mode.

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

The important point is that discovery is not permissive. Most candidates are rejected before scoring matters.

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

The qualification chain is:

1. minimum 90-day trade count
2. minimum win rate
3. minimum profit factor
4. minimum average size
5. maximum days since last trade
6. maximum consecutive losses
7. minimum wallet age
8. anti-lottery filter using median vs mean PnL

The anti-lottery rule is notable:

- if median PnL is negative but mean PnL is positive over a sufficient sample, the wallet is treated as a lottery-style profile and rejected

This is one of the stronger quality-preserving heuristics in the system.

### 5.4 Fallback heuristics

Polymarket leaderboard does not always provide all desired metrics directly.

The bot therefore estimates:

- `monthly_pnl_pct` from `pnl / volume` when direct ROI-like values are absent
- `win_rate_hint` from positive `pnl / vol`
- `profit_factor_hint` from `(vol + pnl) / (vol - pnl)`
- `wallet_age_days` from volume, trade count, leaderboard rank, and multi-category presence

These heuristics are operationally useful, but they also reduce metric fidelity.

Most important consequence:

- in production, many top-ranked wallets collapse onto nearly identical `win_rate` values such as `0.75`
- this means win rate often acts as a threshold gate, not a strong rank differentiator

## 6. Score Formula

Once a wallet survives all hard filters, it receives a score:

```text
score =
    win_rate * 120
  + monthly_pnl_pct * 80
  + trades_30d_count * 0.6
  + avg_size / 100
  + profit_factor * 25
  - days_since_last * 3
  - consecutive_losses * 15
```

Interpretation:

- `win_rate` rewards consistency
- `monthly_pnl_pct` rewards efficiency of capital usage
- `trades_30d_count` rewards current activity
- `avg_size` rewards larger conviction / larger bankroll wallets
- `profit_factor` rewards quality of outcome distribution
- `days_since_last` penalizes stale wallets
- `consecutive_losses` penalizes recent negative streaks

### 6.1 Current bias inside the formula

The current score is materially biased toward `avg_size`.

Reason:

- `avg_size / 100` grows linearly without a hard cap
- large wallets can gain hundreds or thousands of score points from size alone
- `win_rate` and `profit_factor` contribute much smaller absolute ranges

Practical consequence:

- ranking behaves more like `quality filter + whale sort`
- the score is useful for excluding weak wallets, but less reliable for fine ordering among strong ones

## 7. Final Selection

After scoring:

- wallets are sorted descending by `score`
- final enabled set is simply `scored[:enabled_limit]`

There is currently no diversification rule in the final selected set.

That means:

- no cap by category
- no cap by behavior profile
- no cap by tradability history

This is one of the biggest structural characteristics of the current strategy.

## 8. Trade Monitoring Logic

`core/trade_monitor.py` transforms selected wallets into actual copyable intents.

### 8.1 Wallet scan

Monitor loads enabled wallets from the database:

- sorted by score descending
- limited by current risk mode

For each enabled wallet:

- fetch latest wallet trades
- ignore any trade whose `external_trade_id` already exists locally
- enforce optional filters:
  - short-term market toggle
  - price filter toggle

### 8.2 Intent shaping

Each trade becomes a `TradeIntent` carrying:

- wallet metadata: score, win rate, PF, average position size
- market metadata: `market_id`, `token_id`, `outcome`
- source execution metadata: side, price, size

There is also an important anti-spam control:

- `MAX_INTENTS_PER_WALLET = 2`

This prevents a single wallet from flooding one scan cycle with many sequential trades.

### 8.3 Reconcile-close logic

Trade monitor also compares:

- source wallet open positions
- local mirrored open positions

If the source no longer has a position that the local bot still holds, monitor may emit a synthetic reconcile-close intent.

This is a key part of keeping the copy-book aligned with source behavior.

## 9. Risk Logic

`core/risk_manager.py` answers two questions:

1. is this trade allowed at all?
2. if yes, what size should the bot take?

### 9.1 Portfolio-level gates

Before sizing:

- global drawdown stop can block all trading
- daily drawdown stop can block all trading
- optional price filter can reject the trade

### 9.2 Aggressive mode sizing

Aggressive mode is the main live mode right now.

Core caps:

- `per_wallet_cap = capital * max_per_wallet_pct`
- `per_position_cap = capital * max_per_position_pct`
- `total_exposure_cap = capital * max_total_exposure_pct`

Wallet multiplier:

```text
wallet_multiplier = 0.8 + min(max(wallet_score, 0.0), 1.5) * 0.7
```

Then:

- if source size is more than 2x wallet average trade size and boost is enabled, apply `high_conviction_multiplier`
- cap multiplier at `2.5`

Light Kelly:

```text
kelly = max(((b * p) - q) / b, 0.0)
light_kelly_fraction = min(kelly * 0.25 * kelly_multiplier, 0.22)
```

Where:

- `p = win_rate`
- `q = 1 - p`
- `b = clipped profit_factor`

Raw size:

```text
raw_target_size = source_size_usd * wallet_multiplier + capital * kelly_fraction
```

Then final target size is capped by:

- per-position cap
- remaining wallet capacity
- remaining total capacity
- available cash

Important strategic nuance:

- selection ranking uses the full score range
- risk sizing clips `wallet_score` at `1.5`

So huge score differences matter a lot for ranking, but much less for position sizing.

### 9.3 Conservative mode sizing

Conservative mode is intentionally simpler:

- size is `source_size_usd * 0.15`
- smaller caps
- no Kelly component

## 10. Trade Execution Logic

`core/trade_executor.py` is the point where strategic signals meet market reality.

### 10.1 Pre-execution safety

Before sending an order:

- duplicate intent insertion is guarded via `external_trade_id`
- `SELL` without confirmed local position is skipped
- market-position precheck can trim or reject size
- too-small residual position is skipped
- manual approval can be required for large tickets

### 10.2 Execution plan

Execution plan currently depends on:

- source side
- risk mode
- slippage limits
- fill mode

The live system has already been modified to be more tolerant on `BUY` than it was originally, but execution still materially constrains realized performance.

### 10.3 Post-submission lifecycle

A trade can become:

- `submitted`
- `partial`
- `filled`
- `canceled`
- `expired`
- `skipped`
- `failed`

These are then reconciled by:

- fill monitor
- stale-order cleanup
- account position sync

## 11. Current Live Snapshot

Live status snapshot used for this document:

- `risk_mode = aggressive`
- `tracked_wallets = 14`
- `discovery_scanned_candidates = 153`
- `discovery_passed_filters = 14`
- `total_equity_usd = 62.6920`
- `exposure_usd = 20.4329`
- `available_cash_usd = 35.3093`
- `open_order_reserve_usd = 6.9498`
- `daily_pnl_usd = -10.4912`
- `cumulative_pnl_usd = -6.0277`
- `daily_drawdown_pct = 14.34%`

Interpretation:

- discovery is working and not empty
- the bot is live, active, and carrying real positions
- current realized / marked-to-market performance is weak

## 12. Live Trade Quality Snapshot

Based on the latest 80 live trades:

### 12.1 Status distribution

- `filled = 2`
- `failed = 31`
- `skipped = 41`
- `canceled = 4`
- `expired = 2`

Approximate fill rate over this window:

- `2 / 80 = 2.5%`

### 12.2 Main failure reasons

- `21` `orderbook_not_found`
- `20` `no orders found to match with FAK order`
- `11` `insufficient_balance_allowance`
- `9` `float division by zero`
- `4` `canceled_without_fill`
- `3` `market_position_cap_reached`

### 12.3 Category mix

- `Politics = 30`
- `Sports = 22`
- `Crypto = 18`
- `Iran = 10`

### 12.4 Wallet concentration in the latest 80 trades

- `0x20a2f72aeb2f1643752ee380adacbfaa04ecdc15` -> `25`
- `0x0d15e2724df455f9e65b15104d21d76412a2c454` -> `18`
- `0x36946572d760b5b1a50cfcd213ba901f1cd4c5ae` -> `18`
- `0xce71bed9dd1418abc34d244730879d795e8683bc` -> `16`
- `0xc65ca4755436f82d8eb461e65781584b8cadea39` -> `3`

This means `77 / 80` recent trade attempts came from just 4 wallets.

## 13. Practical Review of the Active Wallet Subset

This section is not a ranking of all selected wallets. It is a runtime review of the wallets that are actually driving the recent live tape.

### 13.1 `0x20a2...dc15`

Current reading:

- the most important recent source
- produced both actual fills and multiple failures
- drives a lot of Sports and Iran flow

Interpretation:

- operationally useful because it generates live opportunities
- operationally noisy because it also drives thin and low-price markets
- best described as a high-signal but dirty source

### 13.2 `0x0d15...c454`

Current reading:

- heavily active
- a large share of recent attempts fail with `FAK no match`

Interpretation:

- not necessarily a bad trader
- but in the current runtime profile this wallet produces many signals that the bot cannot reproduce at acceptable price / liquidity

### 13.3 `0x3694...c5ae`

Current reading:

- almost entirely routes the bot into short-term crypto
- recent stream is dominated by `orderbook_not_found`

Interpretation:

- currently toxic for this runtime
- not because the wallet is necessarily bad
- but because the bot cannot convert its flow into executable orders reliably

### 13.4 `0xce71...83bc`

Current reading:

- many recent `SELL`-oriented attempts
- often collides with `insufficient_balance_allowance`

Interpretation:

- strategy match is poor unless local inventory alignment is clean
- manual user closes and source/local desync make this wallet harder to mirror than a buy-dominant source

## 14. Strengths of the Current Trading Logic

The current strategy is stronger than a naive copy bot in several ways:

1. Discovery is strict.
   - only a small minority of candidates survive

2. It is multi-factor.
   - win rate, PF, activity, recency, age, losses, and size all matter

3. It has anti-lottery protection.
   - median-negative / mean-positive behavior is explicitly filtered

4. It separates wallet selection from risk sizing.
   - this allows good sources to remain selected even if ticket size is constrained

5. It supports source-position reconciliation.
   - this is important for staying aligned with copied wallets over time

## 15. Weaknesses of the Current Trading Logic

These are the biggest strategic weaknesses as of the current GitHub version.

### 15.1 Ranking is too size-sensitive

The score overweights `avg_size`, which makes large bankrolls dominate ranking even when quality edge is not proportionally higher.

### 15.2 Key metrics are partly heuristic

When Polymarket does not provide direct win rate and profit factor, the bot estimates them from `pnl / volume`.

This is useful, but it means:

- rank order is partly inferred, not observed
- many strong wallets collapse into very similar metric values

### 15.3 Final selected set is not diversification-aware

The bot simply takes the top `N` by score.

It does not ask:

- are these wallets too behaviorally similar?
- do they over-concentrate on one category?
- do they produce signals the current executor can realistically trade?

### 15.4 Selection is not tradability-aware

Current selection quality is judged before the execution layer has its say.

In practice, some wallets route the bot into:

- nonexistent orderbooks
- ultra-thin markets
- precision edge cases
- low-price markets that the current order builder mishandles

This is the biggest gap between theoretical source quality and realized bot quality.

## 16. Current End-to-End Assessment

### 16.1 Wallet selection alone

Assessment: `6.5 / 10`

Why:

- good structure
- strict filtering
- meaningful anti-noise gates
- but ranking is distorted by size and proxy metrics

### 16.2 Live realized trading

Assessment: `3 / 10`

Why:

- the wallet selection engine is producing signals
- but too few signals become high-quality fills
- live PnL is currently weak

## 17. High-Impact Levers Inside the Strategy

These are the three highest-impact strategy levers identified during review. They are listed here for analysis only, not as approved changes.

1. reduce the dominance of `avg_size` in score
2. add tradability-aware selection feedback
3. add diversification constraints to the final enabled set

These are strategy-level changes and should not be implemented without explicit approval.

## 18. Non-Strategy Runtime Issues That Distort Evaluation

These issues are outside the wallet-selection thesis, but they materially distort performance reading:

1. low-price execution bug in `data/polymarket_client.py`
   - very cheap markets can round to `0.00`, causing `float division by zero`

2. missing or thin orderbooks
   - especially in short-term crypto and some sports markets

3. inventory / allowance mismatch on some sells
   - worsened when positions are manually closed outside the bot

Without controlling for these, it is hard to judge the strategy purely by PnL.

## 19. Recommended Reading Order for Future Review

If reviewing the strategy from scratch, use this order:

1. `config/settings.py`
2. `core/wallet_discovery.py`
3. `core/trade_monitor.py`
4. `core/risk_manager.py`
5. `core/trade_executor.py`
6. `data/polymarket_client.py`
7. `core/background_tasks.py`

This order moves from thesis to signal generation to execution reality.

## 20. Bottom Line

The core idea of the bot is intact:

- discover strong Polymarket wallets
- filter aggressively
- mirror only a selected subset
- size trades according to portfolio constraints and wallet quality

What is working:

- strict qualification
- nontrivial filtering
- live signal generation

What is not yet working well enough:

- accurate fine-grained ranking among good wallets
- translation of selected-wallet flow into executable, profitable live trades

The current system is therefore best described as:

- a strategy engine with a meaningful selection thesis
- paired with an execution layer that still prevents that thesis from fully expressing itself in production
