# Cloud Deployment Guide (24/7 Autonomous)

This project can run fully autonomous in cloud without your local machine.

## Stack

- SmartBrain API service (`services/smartbrain-python`)
- Supabase (market/feature/trade/settings storage)
- External model storage on persistent disk (`/var/data/model_store`)
- Internal scheduler (`APScheduler`) inside SmartBrain API process

## Render Blueprint

Use:

- `infra/cloud/render/smartbrain-stack.yaml`

It deploys:

1. `smartbrain-service` (FastAPI + internal autonomous scheduler)

## After deploy

1. Set env vars for service (API keys, Supabase, Hyperliquid, scheduler flags).
2. Run bootstrap:

```bash
make smartbrain-bootstrap
```

3. Confirm:
- SmartBrain health: `https://<smartbrain-host>/health`
- `smartbrain_logs` receives `internal_scheduler` records every minute/five minutes
- `smartbrain_settings.enabled=true` in Supabase

## Safety default

Bootstrap defaults to:

- `enabled=true`
- `paper_mode=true`

So autonomous loop runs in paper mode first. Switch to live only after validation.
