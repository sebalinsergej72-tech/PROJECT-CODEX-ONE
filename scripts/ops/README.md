# SmartBrain Ops Bootstrap

## What it does

`bootstrap_smartbrain.py` performs four actions:

1. Applies Supabase SQL migration:
- `infra/supabase/migrations/20260218_smartbrain_init.sql`

2. Initializes/updates SmartBrain settings row (optional, enabled by default):
- enables autonomous mode flags
- sets paper/live mode and risk caps
- applies whitelist

3. Optional: upserts all n8n workflows and optionally activates them:
- `workflows/n8n/01_smartbrain_data_ingestion.json`
- `workflows/n8n/02_smartbrain_decision_cycle.json`
- `workflows/n8n/03_smartbrain_daily_retraining.json`
- `workflows/n8n/04_smartbrain_hourly_tree_update.json`
- `workflows/n8n/05_smartbrain_rl_replay_30m.json`

4. Optionally verifies SmartBrain service `/health`.

For budget mode, you can skip n8n env vars entirely. The internal scheduler in `smartbrain-service` handles autonomous loops.

## Migration fallback order

The script executes SQL in this order:

1. `psycopg` (if installed)
2. `supabase db query --db-url ... --file ...` (if `supabase` CLI installed)
3. `psql <DB_URL> -f <migration.sql>` (if `psql` installed)

## Required env vars

- `SMARTBRAIN_SUPABASE_DB_URL`

## Optional env vars

- `SMARTBRAIN_N8N_BASE_URL` (set together with `SMARTBRAIN_N8N_API_KEY` only if you still use n8n mode)
- `SMARTBRAIN_N8N_API_KEY`
- `SMARTBRAIN_N8N_ACTIVATE` (`true|false`, default `true`)
- `SMARTBRAIN_SERVICE_URL` (for health check)
- `SMARTBRAIN_INIT_SETTINGS` (`true|false`, default `true`)
- `SMARTBRAIN_INIT_ENABLED` (`true|false`, default `true`)
- `SMARTBRAIN_INIT_PAPER_MODE` (`true|false`, default `true`)
- `SMARTBRAIN_INIT_RISK_LEVEL` (`low|medium|high`, default `medium`)
- `SMARTBRAIN_INIT_MAX_LEVERAGE` (default `3`)
- `SMARTBRAIN_INIT_MAX_PORTFOLIO_RISK` (default `0.02`)
- `SMARTBRAIN_INIT_MAX_DRAWDOWN_CIRCUIT` (default `0.08`)
- `SMARTBRAIN_INIT_ASSETS` (comma list, default `BTC,ETH`)
- `SMARTBRAIN_INIT_EMERGENCY_STOP` (`true|false`, default `false`)

## Run

```bash
SMARTBRAIN_OPS_ENV_FILE=/tmp/smartbrain.ops.env ./scripts/ops/bootstrap_smartbrain.sh
```

Optional dependency install mode (creates `.venv-ops` and installs `scripts/ops/requirements.txt`):

```bash
SMARTBRAIN_BOOTSTRAP_INSTALL_DEPS=true SMARTBRAIN_OPS_ENV_FILE=/tmp/smartbrain.ops.env ./scripts/ops/bootstrap_smartbrain.sh
```
