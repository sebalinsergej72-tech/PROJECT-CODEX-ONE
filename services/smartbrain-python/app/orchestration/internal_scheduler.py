from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.models.schemas import ExecutionRequest, RiskCheckRequest
from app.services.decision_engine import decision_engine
from app.services.execution_service import execution_service
from app.services.feature_engineering import feature_engineering_service
from app.services.ingestion_service import ingestion_service
from app.services.online_learning_service import online_learning_service
from app.services.performance_service import performance_service
from app.services.risk_service import risk_service
from app.services.settings_service import settings_service
from app.services.state_service import state_service
from app.services.training_service import training_service
from app.storage.supabase_repo import repo


class InternalAutonomyScheduler:
    def __init__(self) -> None:
        self.logger = logging.getLogger("smartbrain.internal_scheduler")
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self._started = False
        self._cycle_lock = asyncio.Lock()

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _log_event(self, level: str, message: str, context: dict[str, Any] | None = None) -> None:
        payload = {
            "level": level,
            "message": message,
            "context": {
                "source": "internal_scheduler",
                "ts": self._utc_now(),
                **(context or {}),
            },
        }
        try:
            repo.insert("smartbrain_logs", payload)
        except Exception as exc:  # pragma: no cover - best effort
            self.logger.warning("Failed to write scheduler log row: %s", exc)

    async def _job_ingestion(self) -> None:
        try:
            records = ingestion_service.ingest_snapshot()
            vectors = feature_engineering_service.materialize(
                symbols=settings.ingest_symbols,
                timeframes=settings.ingest_timeframes,
            )
            self._log_event(
                "info",
                "Internal ingestion cycle completed",
                {"records_written": records, "vectors_written": vectors},
            )
        except Exception as exc:
            self.logger.exception("Ingestion job failed")
            self._log_event("error", "Internal ingestion cycle failed", {"error": str(exc)})

    async def _job_decision_cycle(self) -> None:
        if self._cycle_lock.locked():
            self._log_event("warn", "Decision cycle skipped: previous cycle still running")
            return

        async with self._cycle_lock:
            try:
                active = settings_service.get_active()
                if not active.get("enabled", False):
                    self._log_event("info", "Decision cycle skipped: smartbrain disabled")
                    return
                if active.get("emergency_stop", False):
                    self._log_event("warn", "Decision cycle skipped: emergency stop active")
                    return

                mode = "paper" if active.get("paper_mode", True) else "live"
                if mode == "live":
                    perf = performance_service.refresh_live()
                    drawdown = float(perf.get("max_drawdown", 0.0) or 0.0)
                    max_dd = float(active.get("max_drawdown_circuit", 0.08) or 0.08)
                    if drawdown >= max_dd:
                        self._log_event(
                            "critical",
                            "Decision cycle blocked by drawdown circuit breaker",
                            {"drawdown": drawdown, "max_drawdown_circuit": max_dd},
                        )
                        return

                symbols = [str(s).upper() for s in (active.get("assets_whitelist", []) or []) if str(s).strip()]
                if not symbols:
                    self._log_event("warn", "Decision cycle skipped: empty assets whitelist")
                    return

                success_count = 0
                blocked_count = 0
                for symbol in symbols:
                    risk = risk_service.check(
                        RiskCheckRequest(symbol=symbol, mode=mode, requested_leverage=1)
                    )
                    if not risk.allowed:
                        blocked_count += 1
                        self._log_event(
                            "warn",
                            f"Execution blocked by risk for {symbol}",
                            {"mode": mode, "reasons": risk.reasons},
                        )
                        continue

                    state = state_service.build_state(symbol=symbol, mode=mode)
                    decision = decision_engine.make_decision(state=state, max_leverage=risk.max_leverage)
                    execution = execution_service.execute(
                        ExecutionRequest(symbol=symbol, mode=mode, decision=decision, state=state),
                        max_portfolio_risk=risk.max_position_pct,
                    )
                    if execution.success:
                        success_count += 1

                self._log_event(
                    "info",
                    "Decision cycle completed",
                    {
                        "mode": mode,
                        "symbols": symbols,
                        "success_count": success_count,
                        "blocked_count": blocked_count,
                    },
                )
            except Exception as exc:
                self.logger.exception("Decision cycle job failed")
                self._log_event("error", "Decision cycle failed", {"error": str(exc)})

    async def _job_hourly_tree_update(self) -> None:
        try:
            result = online_learning_service.run_tree_incremental_update()
            self._log_event("info", "Hourly online tree update completed", result)
        except Exception as exc:
            self.logger.exception("Hourly tree update failed")
            self._log_event("error", "Hourly online tree update failed", {"error": str(exc)})

    async def _job_rl_replay_update(self) -> None:
        try:
            result = online_learning_service.run_rl_replay_update()
            level = "info" if result.get("status") == "updated" else "warn"
            self._log_event(level, "RL replay update completed", result)
        except Exception as exc:
            self.logger.exception("RL replay update failed")
            self._log_event("error", "RL replay update failed", {"error": str(exc)})

    async def _job_daily_retraining(self) -> None:
        try:
            perf = performance_service.refresh_live()
            version, promoted = training_service.retrain_daily()
            self._log_event(
                "info",
                "Daily retraining completed",
                {"performance": perf, "new_version": version, "promoted": promoted},
            )
        except Exception as exc:
            self.logger.exception("Daily retraining failed")
            self._log_event("error", "Daily retraining failed", {"error": str(exc)})

    def _register_jobs(self) -> None:
        self.scheduler.add_job(
            self._job_ingestion,
            IntervalTrigger(minutes=settings.scheduler_ingestion_interval_minutes),
            id="smartbrain-ingestion-5m",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=90,
        )
        self.scheduler.add_job(
            self._job_decision_cycle,
            IntervalTrigger(minutes=settings.scheduler_decision_interval_minutes),
            id="smartbrain-decision-1m",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=45,
        )
        self.scheduler.add_job(
            self._job_hourly_tree_update,
            IntervalTrigger(hours=settings.scheduler_tree_interval_hours),
            id="smartbrain-online-tree-hourly",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=600,
        )
        self.scheduler.add_job(
            self._job_rl_replay_update,
            IntervalTrigger(minutes=settings.scheduler_rl_replay_interval_minutes),
            id="smartbrain-rl-replay-30m",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=600,
        )
        self.scheduler.add_job(
            self._job_daily_retraining,
            CronTrigger(
                hour=settings.scheduler_daily_retrain_hour_utc,
                minute=settings.scheduler_daily_retrain_minute_utc,
                timezone="UTC",
            ),
            id="smartbrain-daily-retrain-utc",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=1800,
        )

    async def start(self) -> None:
        if self._started:
            return
        self._register_jobs()
        self.scheduler.start()
        self._started = True
        self._log_event(
            "info",
            "Internal scheduler started",
            {
                "ingestion_minutes": settings.scheduler_ingestion_interval_minutes,
                "decision_minutes": settings.scheduler_decision_interval_minutes,
                "rl_replay_minutes": settings.scheduler_rl_replay_interval_minutes,
                "tree_hours": settings.scheduler_tree_interval_hours,
                "daily_hour_utc": settings.scheduler_daily_retrain_hour_utc,
                "daily_minute_utc": settings.scheduler_daily_retrain_minute_utc,
            },
        )

    async def stop(self) -> None:
        if not self._started:
            return
        self.scheduler.shutdown(wait=False)
        self._started = False
        self._log_event("info", "Internal scheduler stopped")


internal_scheduler = InternalAutonomyScheduler()

