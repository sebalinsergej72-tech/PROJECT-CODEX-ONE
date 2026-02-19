# SmartBrain Trading Engine Scaffold

This repository contains implementation slices for SmartBrain:

- `apps/web`: UI integration skeleton with a new `SmartBrain` control tab.
- `services/smartbrain-python`: FastAPI microservice for ingestion, features, inference, risk checks, execution, performance tracking, and retraining.
- `workflows/n8n`: legacy n8n workflow JSON definitions (optional, not required).
- `infra/supabase/migrations`: SQL migration for SmartBrain tables and settings.
- `infra/cloud/render/smartbrain-stack.yaml`: Render blueprint for low-cost single-service cloud mode.

## Budget autonomous mode (primary)

SmartBrain now runs autonomous orchestration in-process via internal scheduler (`APScheduler`) inside the API service.
No n8n and no Temporal are required.

Scheduler jobs:

1. Data ingestion every 5 minutes: `/ingest/snapshot`, `/features/materialize`.
2. Decision cycle every 1 minute: `/settings/active` + `/autonomous/cycle` per asset.
3. Online tree update every 1 hour: `/training/online-tree`.
4. RL replay update every 30 minutes: `/training/rl-replay`.
5. Daily retraining at 00:00 UTC: `/performance/refresh`, `/training/daily`.

The decision cycle includes a global drawdown circuit breaker before execution in live mode.

Scheduler code:
- `services/smartbrain-python/app/orchestration/internal_scheduler.py`

## Online learning behavior

- `XGBoost` online model updates hourly from labeled `experience_replay`.
- `SAC` RL policy runs replay-buffer style updates every 30 minutes.
- Active model versions are auto-promoted with simple degradation guardrails.

## Bootstrap automation

Use `scripts/ops/bootstrap_smartbrain.py` to:

- apply Supabase migration,
- initialize SmartBrain settings (enabled, paper/live, risk caps, whitelist),
- verify SmartBrain health.

Templates:

- `scripts/ops/.env.ops.example`
- `scripts/ops/.env.example`

Run:

```bash
make smartbrain-bootstrap
```
