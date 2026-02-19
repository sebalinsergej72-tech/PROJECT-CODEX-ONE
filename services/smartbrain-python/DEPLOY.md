# SmartBrain Python Service Deployment

## 1) Local smoke test

```bash
cd services/smartbrain-python
cp .env.example .env
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Health check:

```bash
curl http://localhost:8000/health
```

## 2) Deploy on Render

1. Create new Web Service from this repository.
2. Root directory: `services/smartbrain-python`.
3. Runtime: Docker (uses `Dockerfile`).
4. Add environment variables from `.env.example`.
5. Deploy and copy service URL.

For full cloud stack (single-service budget mode), use:

- `infra/cloud/render/smartbrain-stack.yaml`

## 3) Apply Supabase migration

Run SQL from:

- `infra/supabase/migrations/20260218_smartbrain_init.sql`

Or run one bootstrap command that applies migration and initializes settings:

```bash
cp scripts/ops/.env.ops.example /tmp/smartbrain.ops.env
# fill values in /tmp/smartbrain.ops.env, then:
set -a; source /tmp/smartbrain.ops.env; set +a
export SMARTBRAIN_SUPABASE_DB_URL="postgresql://..."
export SMARTBRAIN_SERVICE_URL="https://smartbrain-service.example.com"
SMARTBRAIN_OPS_ENV_FILE=/tmp/smartbrain.ops.env ./scripts/ops/bootstrap_smartbrain.sh
# optional with dependency install mode:
# SMARTBRAIN_BOOTSTRAP_INSTALL_DEPS=true SMARTBRAIN_OPS_ENV_FILE=/tmp/smartbrain.ops.env ./scripts/ops/bootstrap_smartbrain.sh
# or:
make smartbrain-bootstrap
```

Env template: `scripts/ops/.env.ops.example`.

## 4) Enable internal autonomous scheduler

Set these env vars on `smartbrain-service`:

- `INTERNAL_SCHEDULER_ENABLED=true`
- `SCHEDULER_INGESTION_INTERVAL_MINUTES=5`
- `SCHEDULER_DECISION_INTERVAL_MINUTES=1`
- `SCHEDULER_RL_REPLAY_INTERVAL_MINUTES=30`
- `SCHEDULER_TREE_INTERVAL_HOURS=1`
- `SCHEDULER_DAILY_RETRAIN_HOUR_UTC=0`
- `SCHEDULER_DAILY_RETRAIN_MINUTE_UTC=0`

After deploy/restart, this single service runs all autonomous cycles in the background.

## 5) What runs automatically

1. Ingestion + feature materialization every 5 min.
2. Decision cycle every 1 min (with drawdown circuit breaker).
3. RL replay update every 30 min.
4. Online tree update every 1 hour.
5. Daily retraining at 00:00 UTC.

## 6) API endpoints still available

- `GET /settings/active`
- `POST /performance/refresh`
- `POST /ingest/snapshot`
- `POST /features/materialize`
- `POST /autonomous/cycle`
- `POST /training/online-tree`
- `POST /training/rl-replay`
- `POST /training/daily`

## 7) Connect with existing app/Supabase secrets

Set in Supabase project secrets/config:

- `SMARTBRAIN_SERVICE_URL`
- `SMARTBRAIN_API_KEY`

UI backend endpoints should read/write `smartbrain_settings`, `smartbrain_logs`, and `smartbrain_performance`.
