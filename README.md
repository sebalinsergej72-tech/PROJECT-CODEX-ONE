# SmartBrain Trading Engine Scaffold

This repository contains implementation slices for SmartBrain:

- `apps/web`: UI integration skeleton with a new `SmartBrain` control tab.
- `workflows/n8n`: n8n workflow JSON definitions.
- `services/smartbrain-python`: FastAPI microservice for ingestion, features, inference, risk checks, execution, performance tracking, and retraining.
- `services/n8n`: n8n container image for cloud orchestration.
- `infra/supabase/migrations`: SQL migration for SmartBrain tables and settings.
- `infra/cloud/render/smartbrain-stack.yaml`: Render blueprint for full cloud stack.

## Included workflows

1. `01_smartbrain_data_ingestion.json`
- Scheduled ingestion every 5 minutes.
- Calls `/ingest/snapshot` and `/features/materialize`.

2. `02_smartbrain_decision_cycle.json`
- Scheduled decision cycle every minute.
- Pulls active settings from `/settings/active`.
- Expands `assets_whitelist` and runs `/autonomous/cycle` per asset.

3. `03_smartbrain_daily_retraining.json`
- Daily retraining at 00:00 UTC.
- Calls `/performance/refresh` then `/training/daily`.

4. `04_smartbrain_hourly_tree_update.json`
- Hourly incremental tree update.
- Calls `/training/online-tree`.

5. `05_smartbrain_rl_replay_30m.json`
- RL replay refresh every 30 minutes.
- Calls `/training/rl-replay`.

## Online learning behavior

- `XGBoost` online model updates hourly from labeled `experience_replay`.
- `SAC` RL policy runs replay-buffer style updates every 30 minutes.
- Active model versions are auto-promoted with simple degradation guardrails.

## Bootstrap automation

Use `scripts/ops/bootstrap_smartbrain.py` to:

- apply Supabase migration,
- initialize SmartBrain settings,
- import/activate n8n workflows,
- verify SmartBrain health.

Templates:

- `scripts/ops/.env.ops.example`
- `scripts/ops/.env.example`

Run:

```bash
make smartbrain-bootstrap
```
