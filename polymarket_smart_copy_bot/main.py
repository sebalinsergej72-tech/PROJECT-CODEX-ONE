from __future__ import annotations

import asyncio
import signal
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import typer
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from loguru import logger
from rich.console import Console
from rich.traceback import install as rich_traceback_install

from api.health import router as health_router
from api.control import router as control_router
from api.dashboard import router as dashboard_router
from api.positions import router as positions_router
from api.status import router as status_router
from api.trades import router as trades_router
from config.settings import settings
from core.background_tasks import BackgroundOrchestrator
from data.database import check_database_connection, run_alembic_upgrade
from utils.helpers import graceful_wait

console = Console()
cli = typer.Typer(no_args_is_help=False)


def configure_logging() -> None:
    """Configure loguru logging to stdout for Railway-friendly logs."""

    logger.remove()
    logger.add(
        sys.stdout,
        level=settings.log_level.upper(),
        enqueue=False,
        backtrace=settings.debug,
        diagnose=settings.debug,
    )
    rich_traceback_install(show_locals=settings.debug)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info("Starting {} in {} mode", settings.app_name, settings.app_env)
    if settings.risk_mode == "aggressive":
        logger.warning(
            "🚀 AGGRESSIVE MODE ENABLED | Capital: ${:.2f} | Risk per wallet: {:.0f}% | Kelly x{:.1f}",
            settings.default_starting_equity,
            settings.max_per_wallet_pct * 100,
            settings.kelly_multiplier,
        )

    app.state.started_at = datetime.now(tz=timezone.utc)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, run_alembic_upgrade)
    await check_database_connection()

    orchestrator = BackgroundOrchestrator()
    app.state.orchestrator = orchestrator

    await orchestrator.start()
    startup_status = await orchestrator.get_status()
    discovery = startup_status.get("last_discovery_stats", {})
    logger.info(
        "Startup discovery status | candidates={} passed_all={} enabled={} fallback={} ({})",
        discovery.get("total_candidates", 0),
        discovery.get("passed_all_filters", 0),
        discovery.get("enabled_wallets", 0),
        discovery.get("used_seed_fallback", False),
        discovery.get("seed_fallback_wallets", 0),
    )

    try:
        yield
    finally:
        logger.info("Shutting down background services")
        await orchestrator.stop()


app = FastAPI(title=settings.app_name, lifespan=lifespan)

dashboard_dist = Path(__file__).resolve().parent / "dist"
if dashboard_dist.exists():
    app.mount(
        "/dashboard-static",
        StaticFiles(directory=str(dashboard_dist), html=False),
        name="dashboard-static",
    )

app.include_router(health_router)
app.include_router(status_router)
app.include_router(trades_router)
app.include_router(positions_router)
app.include_router(control_router)
app.include_router(dashboard_router)


async def run_worker() -> None:
    """Run bot components without HTTP API (local CLI mode)."""

    configure_logging()
    logger.info("Running worker mode (no FastAPI server)")

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, run_alembic_upgrade)
    await check_database_connection()

    orchestrator = BackgroundOrchestrator()
    await orchestrator.start()
    startup_status = await orchestrator.get_status()
    discovery = startup_status.get("last_discovery_stats", {})
    logger.info(
        "Worker startup discovery status | candidates={} passed_all={} enabled={} fallback={} ({})",
        discovery.get("total_candidates", 0),
        discovery.get("passed_all_filters", 0),
        discovery.get("enabled_wallets", 0),
        discovery.get("used_seed_fallback", False),
        discovery.get("seed_fallback_wallets", 0),
    )

    stop_event = asyncio.Event()

    def _signal_handler(*_: object) -> None:
        logger.warning("Termination signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stop_event.set())

    await stop_event.wait()
    await orchestrator.stop()
    await graceful_wait(0.2)


@cli.command()
def run(
    with_api: bool = typer.Option(False, help="Run FastAPI server in local mode"),
    host: str = typer.Option("0.0.0.0", help="Host for local API server"),
    port: int = typer.Option(8000, help="Port for local API server"),
) -> None:
    """Launch bot locally; defaults to background worker + Telegram polling only."""

    if with_api:
        uvicorn.run("main:app", host=host, port=port, reload=False)
        return

    asyncio.run(run_worker())


if __name__ == "__main__":
    if len(sys.argv) == 1:
        run(with_api=False, host="0.0.0.0", port=8000)
    else:
        cli()
