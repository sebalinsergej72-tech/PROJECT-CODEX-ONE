# Cloud Deployment Guide (24/7 Autonomous)

This project can run fully autonomous in cloud without your local machine.

## Stack

- SmartBrain API service (`services/smartbrain-python`)
- n8n orchestrator (`services/n8n`)
- Supabase (market/feature/trade/settings storage)
- External model storage on persistent disk (`/var/data/model_store`)

## Render Blueprint

Use:

- `infra/cloud/render/smartbrain-stack.yaml`

It deploys:

1. `smartbrain-service` (FastAPI)
2. `smartbrain-n8n` (n8n)

## After deploy

1. Set env vars for both services (API keys, DB credentials, SmartBrain secret).
2. Run bootstrap:

```bash
make smartbrain-bootstrap
```

3. Confirm:
- SmartBrain health: `https://<smartbrain-host>/health`
- n8n opens and workflows are active
- `smartbrain_settings.enabled=true` in Supabase

## Safety default

Bootstrap defaults to:

- `enabled=true`
- `paper_mode=true`

So autonomous loop runs in paper mode first. Switch to live only after validation.
