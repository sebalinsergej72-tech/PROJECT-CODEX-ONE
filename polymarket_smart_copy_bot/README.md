# Polymarket Smart Copy Trading Bot

Production-ready async Python bot for selective wallet copy-trading on Polymarket, optimized for Railway deployment.

## Features

- Qualified wallets selective copy-trading (5-12 wallets diversification)
- Strict risk controls (per-trade 6.5%, daily 7%)
- Price filter 20-80 cents
- Dry-run mode
- Telegram alerts + manual approval for large trades
- APScheduler with SQLAlchemy job store (persistent jobs after restart)
- FastAPI endpoints: `/health`, `/status`, `/trades`, `/positions`
- Web dashboard: `/dashboard` (status, PnL, trades, positions, trading toggle)
- PostgreSQL-first (`DATABASE_URL`) with SQLite fallback for local development
- Alembic migrations auto-applied on startup
- Graceful shutdown on SIGTERM

## Project Structure

```text
/polymarket_smart_copy_bot/
├── main.py
├── Dockerfile
├── railway.json
├── alembic.ini
├── alembic/
├── config/
├── core/
├── data/
├── models/
├── api/
├── utils/
├── tests/
├── requirements.txt
├── README.md
└── .dockerignore
```

## Quick Start (Local)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config/.env.example .env
python main.py
```

Run local API too:

```bash
python main.py --with-api --port 8000
```

## Required Environment Variables

- `DATABASE_URL` (Railway Postgres URL; if omitted, local SQLite is used)
- `POLYMARKET_PRIVATE_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- Optional: `POLYMARKET_API_KEY`, `POLYMARKET_PROXY_ADDRESS`
- Optional: `POLYMARKET_VERIFY_SSL=true` (set `false` only for local SSL troubleshooting)
- Optional: `DASHBOARD_WRITE_TOKEN` (required for start/stop trading actions in UI/API)

## Deployment on Railway

### 1. Create Project

1. Open Railway dashboard.
2. Create a new project from your GitHub repository.
3. Set root directory to `polymarket_smart_copy_bot` if repository is monorepo.

### 2. Add PostgreSQL

1. In project services, click **New** -> **Database** -> **PostgreSQL**.
2. Railway will inject `DATABASE_URL` automatically into your app service.

### 3. Configure Variables

Set these in Railway service Variables:

- `APP_ENV=production`
- `DRY_RUN=false` (or `true` for smoke testing)
- `LOG_LEVEL=INFO`
- `TIMEZONE=UTC`
- `POLYMARKET_PRIVATE_KEY=...`
- `POLYMARKET_API_KEY=...` (optional, recommended)
- `TELEGRAM_BOT_TOKEN=...`
- `TELEGRAM_CHAT_ID=...`
- `PRICE_MIN_CENTS=20`
- `PRICE_MAX_CENTS=80`
- `MAX_RISK_PER_TRADE=0.065`
- `MAX_DAILY_RISK=0.07`
- `MIN_QUALIFIED_WALLETS=5`
- `MAX_QUALIFIED_WALLETS=12`

### 4. Build and Start

- Build uses `Dockerfile`.
- Start command on Railway:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

`railway.json` already defines this command and `/health` check.

### 5. Health Check

After deploy, verify:

- `GET /health` returns:

```json
{ "status": "healthy", "uptime": "00:00:12", "tracked_wallets": 8 }
```

- `GET /status` returns scheduler/trading runtime state.
- `GET /trades` returns recent copied trades.
- `GET /positions` returns open (or all) positions.
- `GET /dashboard` returns a live web dashboard.
- `POST /control/trading` toggles trading execution (`enabled: true|false`).

### 6. Observability and Runtime

- All logs go to stdout via Loguru.
- Railway log stream will show job lifecycle, scoring refresh, trade decisions, and errors.
- APScheduler jobs persist in PostgreSQL and recover across restarts.
- Open positions are restored from DB on startup.

## Telegram Commands

- `/status` - bot mode + scheduler + open positions + daily PnL
- `/pnl` - equity, exposure, daily/cumulative PnL
- `/trades [limit]` - последние скопированные сделки
- `/positions [open|all] [limit]` - открытые/все позиции
- `/mode aggressive|conservative` - switch risk profile at runtime
- `/boost on|off` - toggle high-conviction multiplier
- `/pricefilter on|off` - toggle 20-80 cents price filter
- Manual trade approval buttons appear automatically for large trades.

## Web Dashboard

Open `http://127.0.0.1:8000/dashboard` (or your Railway domain `/dashboard`).

It includes:
- runtime status (mode, exposure, PnL, tracked wallets)
- recent copied trades and open positions
- action buttons: `Start Trading` and `Stop Trading`
- runtime switches (no redeploy): risk mode, boost, price filter, discovery auto-add

If `DASHBOARD_WRITE_TOKEN` is set, control actions require this token in UI input (sent as `X-Dashboard-Token` header).

## Aggressive Mode for small capital ($70+)

`RISK_MODE=aggressive` is optimized for very small accounts and higher volatility tolerance.

- Wallet selection: top 3-15 hottest wallets (strict thresholds)
- Per-wallet cap: 15%
- Per-position cap: 10%
- Total exposure cap: 65%
- Kelly multiplier: `x2.0`
- Drawdown stop: `-25%` over 24h
- Price filter is disabled by default and can be re-enabled via `/pricefilter on`
- Auto-reinvest runs every 60 minutes

Warning: this mode can produce large drawdowns quickly and is suitable only for high-risk users.
Use `/mode conservative` after scale-up (bot will suggest this around $300+ equity).

## Migrations

Migrations are automatically executed on startup (`alembic upgrade head`).

Manual run:

```bash
alembic upgrade head
```

## Running Tests

```bash
pytest -q
```

## Notes

- Keep secrets only in Railway Variables, never in repository.
- For live execution ensure token-level data mapping (`token_id`) is correct for markets being copied.
- Validate dry-run behavior before switching to live mode.
