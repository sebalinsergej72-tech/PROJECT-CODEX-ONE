from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from loguru import logger


@dataclass(slots=True)
class WalletConfig:
    address: str
    label: str | None = None
    base_weight: float = 1.0


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def format_uptime(started_at: datetime | None) -> str:
    if started_at is None:
        return "unknown"

    delta = utc_now() - started_at
    total_seconds = int(delta.total_seconds())
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def ensure_price_in_cents(price: float) -> float:
    if price <= 1.0:
        return round(price * 100, 4)
    return round(price, 4)


def load_wallets(path: Path) -> list[WalletConfig]:
    if not path.exists():
        logger.warning("Wallet config file {} does not exist", path)
        return []

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    wallets = data.get("wallets", [])

    parsed: list[WalletConfig] = []
    for wallet in wallets:
        address = str(wallet.get("address", "")).strip()
        if not address:
            continue
        parsed.append(
            WalletConfig(
                address=address,
                label=wallet.get("label"),
                base_weight=float(wallet.get("base_weight", 1.0)),
            )
        )
    return parsed


async def graceful_wait(seconds: float) -> None:
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        return
