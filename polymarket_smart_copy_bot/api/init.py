from __future__ import annotations

from fastapi import APIRouter

from api.control import router as control_router
from api.dashboard import router as dashboard_router
from api.health import router as health_router
from api.positions import router as positions_router
from api.status import router as status_router
from api.trades import router as trades_router

router = APIRouter()
router.include_router(health_router)
router.include_router(status_router)
router.include_router(trades_router)
router.include_router(positions_router)
router.include_router(control_router)
router.include_router(dashboard_router)
