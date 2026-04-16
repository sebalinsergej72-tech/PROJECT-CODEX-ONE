# TG Sales Agent

Telegram-native MVP for an AI sales assistant for small businesses.

The service supports:

- Managed Bot onboarding via Telegram `newbot` links and `managed_bot` updates.
- A branded lead bot per business.
- FAQ/catalog-aware sales replies.
- Lead qualification, handoff summaries, follow-ups metadata, and manager notifications.
- Optional OpenAI answer generation with strict local guardrails.
- Google Sheets / CRM sync through simple webhook endpoints.
- A built-in admin workspace for settings, knowledge, leads, conversations, and a chat simulator.
- External LLM replies via OpenAI or OpenRouter with local guardrails preserved.

## Quick Start

```bash
cd services/tg-sales-agent
cp .env.example .env
npm test
npm run dev
```

Open:

```text
http://localhost:8787
```

The app runs without external dependencies. It uses a JSON file in `data/db.json`
for local MVP storage.

## External AI Providers

The bot works without an external model, but you can enable more natural replies.

### OpenAI

```bash
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4.1-mini
```

### OpenRouter

OpenRouter provides an OpenAI-compatible chat completions endpoint at
`https://openrouter.ai/api/v1/chat/completions`, authenticated with a Bearer API key.
Optional attribution headers are `HTTP-Referer` and `X-Title`/`X-OpenRouter-Title`.

Minimal setup:

```bash
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=openai/gpt-4.1-mini
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1/chat/completions
OPENROUTER_REFERER=https://your-app.example
OPENROUTER_TITLE=TG Sales Agent
```

If both providers are configured, the current code prefers OpenRouter.

## Telegram Setup

1. Create a manager bot in `@BotFather`.
2. Open the manager bot settings in BotFather's Mini App.
3. Enable **Bot Management Mode**.
4. Set `MANAGER_BOT_TOKEN`, `MANAGER_BOT_USERNAME`, `BASE_URL`, and `TELEGRAM_WEBHOOK_SECRET`.
5. Register the manager webhook:

```bash
curl -X POST "https://api.telegram.org/bot$MANAGER_BOT_TOKEN/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://your-public-domain.example/telegram/manager/change-this-webhook-secret"}'
```

You can then open this link from the admin UI:

```text
https://t.me/newbot/{MANAGER_BOT_USERNAME}/{suggested_username}?name={suggested_name}
```

When a user confirms creation, Telegram sends a `managed_bot` update. The service
fetches the token via `getManagedBotToken`, stores it, and registers a webhook for
the managed bot.

## Google Sheets Sync

Create a Google Apps Script web app that accepts JSON `POST` requests and appends
rows to a spreadsheet. Put its URL into the business settings as
`sheetWebhookUrl`. The service posts every new or updated lead to that URL.

Payload shape:

```json
{
  "type": "lead",
  "business": { "id": "biz_...", "name": "Studio" },
  "lead": {
    "id": "lead_...",
    "status": "new",
    "score": 78,
    "summary": "..."
  }
}
```

## Production Notes

- Set `ENCRYPTION_KEY` before storing real managed bot tokens.
- Keep Telegram Business data scoped to the service the business explicitly enabled.
- Add a public privacy policy and `/paysupport` if you add Telegram Stars billing.
- Use a real database before multi-tenant production.
- Run behind HTTPS. Telegram webhooks require a public HTTPS URL.

## Deploy To Render

This repo includes a root-level [`render.yaml`](/Users/nikolay/Documents/New project/render.yaml)
blueprint for a single Node web service rooted at
`services/tg-sales-agent`.

Recommended Render setup:

1. Create a new **Web Service** from this repository.
2. Keep the service root at `services/tg-sales-agent`.
3. Use the included defaults:
   - Build command: `npm install`
   - Start command: `npm start`
   - Health check path: `/api/health`
4. Set these environment variables in Render:
   - `MANAGER_BOT_TOKEN`
   - `MANAGER_BOT_USERNAME`
   - `BASE_URL` set to your Render public URL
   - `TELEGRAM_WEBHOOK_SECRET`
   - `OPENROUTER_API_KEY`
   - `OPENROUTER_MODEL`
   - `ENCRYPTION_KEY`
5. Keep `TELEGRAM_POLLING=false` in production so Telegram uses webhooks.

After the service is live, register the manager webhook:

```bash
curl -X POST "https://api.telegram.org/bot$MANAGER_BOT_TOKEN/setWebhook" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"$BASE_URL/telegram/manager/$TELEGRAM_WEBHOOK_SECRET\"}"
```

Managed bots created by the manager bot will automatically register their own
webhooks through the same public service.

For serious production use, move `DATA_PATH` to a mounted persistent disk or
replace JSON storage with Postgres/Supabase.
