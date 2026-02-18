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

For full cloud stack (SmartBrain + n8n), use:

- `infra/cloud/render/smartbrain-stack.yaml`

## 3) Apply Supabase migration

Run SQL from:

- `infra/supabase/migrations/20260218_smartbrain_init.sql`

Or run one bootstrap command that applies migration and syncs/activates n8n workflows:

```bash
cp scripts/ops/.env.ops.example /tmp/smartbrain.ops.env
# fill values in /tmp/smartbrain.ops.env, then:
set -a; source /tmp/smartbrain.ops.env; set +a
export SMARTBRAIN_SUPABASE_DB_URL="postgresql://..."
export SMARTBRAIN_N8N_BASE_URL="https://n8n.example.com"
export SMARTBRAIN_N8N_API_KEY="..."
export SMARTBRAIN_SERVICE_URL="https://smartbrain-service.example.com"
SMARTBRAIN_OPS_ENV_FILE=/tmp/smartbrain.ops.env ./scripts/ops/bootstrap_smartbrain.sh
# optional with dependency install mode:
# SMARTBRAIN_BOOTSTRAP_INSTALL_DEPS=true SMARTBRAIN_OPS_ENV_FILE=/tmp/smartbrain.ops.env ./scripts/ops/bootstrap_smartbrain.sh
# or:
make smartbrain-bootstrap
```

Env template: `scripts/ops/.env.ops.example`.

## 4) Connect with n8n

1. Set n8n env vars:
- `SMARTBRAIN_SERVICE_URL`
- `SMARTBRAIN_SERVICE_API_KEY`

2. Import workflows:
- `workflows/n8n/01_smartbrain_data_ingestion.json`
- `workflows/n8n/02_smartbrain_decision_cycle.json`
- `workflows/n8n/03_smartbrain_daily_retraining.json`
- `workflows/n8n/04_smartbrain_hourly_tree_update.json`
- `workflows/n8n/05_smartbrain_rl_replay_30m.json`

3. Configure Supabase credentials (`smartbrain-supabase`).
4. For workflow `03_smartbrain_daily_retraining.json`, keep timezone as `UTC`.
5. Activate workflows.

## 5) API endpoints used by workflows

- `GET /settings/active`
- `POST /performance/refresh`
- `POST /ingest/snapshot`
- `POST /features/materialize`
- `POST /autonomous/cycle`
- `POST /training/online-tree`
- `POST /training/rl-replay`
- `POST /training/daily`

## 6) Connect with existing app/Supabase secrets

Set in Supabase project secrets/config:

- `SMARTBRAIN_SERVICE_URL`
- `SMARTBRAIN_SERVICE_API_KEY`

UI backend endpoints should read/write `smartbrain_settings`, `smartbrain_logs`, and `smartbrain_performance`.
