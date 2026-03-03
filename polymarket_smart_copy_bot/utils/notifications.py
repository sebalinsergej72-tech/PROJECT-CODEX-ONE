from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

from loguru import logger
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

RiskMode = Literal["aggressive", "conservative"]

BTN_STATUS = "📊 Статус"
BTN_PNL = "💰 PnL"
BTN_TRADES = "🧾 Сделки"
BTN_POSITIONS = "📦 Позиции"
BTN_MODE_AGGR = "⚡ Режим: Агрессивный"
BTN_MODE_CONS = "🛡 Режим: Консервативный"
BTN_BOOST_ON = "🚀 Буст: Вкл"
BTN_BOOST_OFF = "🧯 Буст: Выкл"
BTN_FILTER_ON = "🎯 Фильтр цены: Вкл"
BTN_FILTER_OFF = "🧩 Фильтр цены: Выкл"
BTN_MENU = "📌 Показать меню"


@dataclass(slots=True)
class ApprovalRequest:
    request_id: str
    expires_at: datetime
    future: asyncio.Future[bool]


class NotificationService:
    """Telegram notifier with commands + Russian keyboard controls."""

    def __init__(self, token: str | None, chat_id: int | None) -> None:
        self._token = token
        self._chat_id = chat_id
        self._application: Application | None = None
        self._pending_approvals: dict[str, ApprovalRequest] = {}
        self._status_provider: Callable[[], Awaitable[dict]] | None = None
        self._pnl_provider: Callable[[], Awaitable[dict]] | None = None
        self._mode_setter: Callable[[RiskMode], Awaitable[dict]] | None = None
        self._boost_setter: Callable[[bool], Awaitable[dict]] | None = None
        self._price_filter_setter: Callable[[bool], Awaitable[dict]] | None = None
        self._top_wallets_provider: Callable[[], Awaitable[list[dict]]] | None = None
        self._add_wallet_handler: Callable[[str], Awaitable[dict]] | None = None
        self._remove_wallet_handler: Callable[[str], Awaitable[dict]] | None = None
        self._discovery_status_provider: Callable[[], Awaitable[dict]] | None = None
        self._autoadd_setter: Callable[[bool], Awaitable[dict]] | None = None
        self._trades_provider: Callable[[int], Awaitable[list[dict]]] | None = None
        self._positions_provider: Callable[[bool, int], Awaitable[list[dict]]] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    def register_status_provider(self, provider: Callable[[], Awaitable[dict]]) -> None:
        self._status_provider = provider

    def register_pnl_provider(self, provider: Callable[[], Awaitable[dict]]) -> None:
        self._pnl_provider = provider

    def register_controls(
        self,
        *,
        mode_setter: Callable[[RiskMode], Awaitable[dict]],
        boost_setter: Callable[[bool], Awaitable[dict]],
        price_filter_setter: Callable[[bool], Awaitable[dict]],
        top_wallets_provider: Callable[[], Awaitable[list[dict]]] | None = None,
        add_wallet_handler: Callable[[str], Awaitable[dict]] | None = None,
        remove_wallet_handler: Callable[[str], Awaitable[dict]] | None = None,
        discovery_status_provider: Callable[[], Awaitable[dict]] | None = None,
        autoadd_setter: Callable[[bool], Awaitable[dict]] | None = None,
        trades_provider: Callable[[int], Awaitable[list[dict]]] | None = None,
        positions_provider: Callable[[bool, int], Awaitable[list[dict]]] | None = None,
    ) -> None:
        self._mode_setter = mode_setter
        self._boost_setter = boost_setter
        self._price_filter_setter = price_filter_setter
        self._top_wallets_provider = top_wallets_provider
        self._add_wallet_handler = add_wallet_handler
        self._remove_wallet_handler = remove_wallet_handler
        self._discovery_status_provider = discovery_status_provider
        self._autoadd_setter = autoadd_setter
        self._trades_provider = trades_provider
        self._positions_provider = positions_provider

    async def start(self) -> None:
        if not self.enabled:
            logger.warning("Telegram is disabled: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")
            return

        self._application = ApplicationBuilder().token(self._token).build()
        self._application.add_handler(CommandHandler("status", self._status_handler))
        self._application.add_handler(CommandHandler("pnl", self._pnl_handler))
        self._application.add_handler(CommandHandler("trades", self._trades_handler))
        self._application.add_handler(CommandHandler("positions", self._positions_handler))
        self._application.add_handler(CommandHandler("mode", self._mode_handler))
        self._application.add_handler(CommandHandler("boost", self._boost_handler))
        self._application.add_handler(CommandHandler("pricefilter", self._price_filter_handler))
        self._application.add_handler(CommandHandler("topwallets", self._topwallets_handler))
        self._application.add_handler(CommandHandler("add", self._add_wallet_handler_cmd))
        self._application.add_handler(CommandHandler("remove", self._remove_wallet_handler_cmd))
        self._application.add_handler(CommandHandler("discovery", self._discovery_handler))
        self._application.add_handler(CommandHandler("autoadd", self._autoadd_handler))
        self._application.add_handler(CommandHandler("menu", self._menu_handler))
        self._application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._button_handler))
        self._application.add_handler(
            CallbackQueryHandler(self._approval_callback, pattern=r"^(approve|reject|wallet_approve|wallet_skip):")
        )

        await self._application.initialize()
        await self._application.start()
        if self._application.updater is not None:
            await self._application.updater.start_polling(drop_pending_updates=True)

        await self._application.bot.set_my_commands(
            [
                BotCommand("status", "Текущий статус бота"),
                BotCommand("pnl", "Текущий PnL"),
                BotCommand("trades", "Последние сделки: /trades [limit]"),
                BotCommand("positions", "Позиции: /positions [open|all] [limit]"),
                BotCommand("mode", "Переключить режим: aggressive|conservative"),
                BotCommand("boost", "Буст: on|off"),
                BotCommand("pricefilter", "Фильтр цены: on|off"),
                BotCommand("topwallets", "Топ кошельков discovery"),
                BotCommand("add", "Добавить кошелёк: /add 0x..."),
                BotCommand("remove", "Отключить кошелёк: /remove 0x..."),
                BotCommand("discovery", "Discovery status"),
                BotCommand("autoadd", "Auto-add new top wallets: on|off"),
                BotCommand("menu", "Показать кнопки управления"),
            ]
        )

        try:
            await self.send_control_panel_message("Панель управления готова. Используйте кнопки ниже.")
        except Exception as exc:
            logger.warning("Failed to send Telegram control panel message: {}", exc)
        logger.info("Telegram polling started")

    async def stop(self) -> None:
        if self._application is None:
            return

        try:
            if self._application.updater is not None:
                await self._application.updater.stop()
            await self._application.stop()
            await self._application.shutdown()
        finally:
            self._application = None
            for request in self._pending_approvals.values():
                if not request.future.done():
                    request.future.set_result(False)
            self._pending_approvals.clear()

    async def send_message(self, message: str) -> None:
        if self._application is None or not self.enabled:
            logger.debug("Telegram message skipped (service disabled): {}", message)
            return

        await self._application.bot.send_message(chat_id=self._chat_id, text=message)

    async def send_control_panel_message(self, text: str) -> None:
        if self._application is None or not self.enabled:
            return
        await self._application.bot.send_message(
            chat_id=self._chat_id,
            text=text,
            reply_markup=self._keyboard(),
        )

    async def request_manual_approval(
        self,
        title: str,
        body: str,
        timeout_seconds: int = 180,
    ) -> bool:
        """Request manual confirmation for a large trade via Telegram buttons."""

        if self._application is None or not self.enabled:
            logger.warning("Manual approval requested but Telegram is disabled; rejecting by default")
            return False

        request_id = uuid4().hex
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=timeout_seconds)
        self._pending_approvals[request_id] = ApprovalRequest(
            request_id=request_id,
            expires_at=expires_at,
            future=future,
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Approve", callback_data=f"approve:{request_id}"),
                    InlineKeyboardButton("Reject", callback_data=f"reject:{request_id}"),
                ]
            ]
        )

        await self._application.bot.send_message(
            chat_id=self._chat_id,
            text=f"{title}\n\n{body}\n\nExpires in {timeout_seconds} sec.",
            reply_markup=keyboard,
        )

        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError:
            logger.warning("Manual approval request {} expired", request_id)
            return False
        finally:
            self._pending_approvals.pop(request_id, None)

    async def request_wallet_approval(
        self,
        title: str,
        body: str,
        timeout_seconds: int = 240,
    ) -> bool:
        """Request manual approval for adding a discovered top wallet."""

        if self._application is None or not self.enabled:
            logger.warning("Wallet approval requested but Telegram is disabled; skipping by default")
            return False

        request_id = uuid4().hex
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=timeout_seconds)
        self._pending_approvals[request_id] = ApprovalRequest(
            request_id=request_id,
            expires_at=expires_at,
            future=future,
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Approve", callback_data=f"wallet_approve:{request_id}"),
                    InlineKeyboardButton("Skip", callback_data=f"wallet_skip:{request_id}"),
                ]
            ]
        )

        await self._application.bot.send_message(
            chat_id=self._chat_id,
            text=f"{title}\n\n{body}\n\nExpires in {timeout_seconds} sec.",
            reply_markup=keyboard,
        )

        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError:
            logger.warning("Wallet approval request {} expired", request_id)
            return False
        finally:
            self._pending_approvals.pop(request_id, None)

    async def _status_handler(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.effective_message.reply_text(await self._build_status_text(), reply_markup=self._keyboard())

    async def _pnl_handler(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.effective_message.reply_text(await self._build_pnl_text(), reply_markup=self._keyboard())

    async def _trades_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        limit = 10
        if context.args:
            try:
                limit = int(context.args[0])
            except ValueError:
                await update.effective_message.reply_text("Использование: /trades [limit]", reply_markup=self._keyboard())
                return
        await update.effective_message.reply_text(await self._build_trades_text(limit=limit), reply_markup=self._keyboard())

    async def _positions_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return

        open_only = True
        limit = 10
        if context.args:
            arg0 = context.args[0].lower()
            if arg0 in {"open", "all"}:
                open_only = arg0 != "all"
                if len(context.args) > 1:
                    try:
                        limit = int(context.args[1])
                    except ValueError:
                        await update.effective_message.reply_text(
                            "Использование: /positions [open|all] [limit]",
                            reply_markup=self._keyboard(),
                        )
                        return
            else:
                try:
                    limit = int(arg0)
                except ValueError:
                    await update.effective_message.reply_text(
                        "Использование: /positions [open|all] [limit]",
                        reply_markup=self._keyboard(),
                    )
                    return

        await update.effective_message.reply_text(
            await self._build_positions_text(open_only=open_only, limit=limit),
            reply_markup=self._keyboard(),
        )

    async def _menu_handler(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.effective_message.reply_text("Кнопки управления ниже.", reply_markup=self._keyboard())

    async def _mode_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self._mode_setter is None:
            await update.effective_message.reply_text("Mode control is not configured", reply_markup=self._keyboard())
            return

        if not context.args or context.args[0].lower() not in {"aggressive", "conservative"}:
            await update.effective_message.reply_text("Использование: /mode aggressive|conservative", reply_markup=self._keyboard())
            return

        mode = context.args[0].lower()
        status = await self._mode_setter(mode)  # type: ignore[arg-type]
        await update.effective_message.reply_text(
            f"Режим риска обновлён: {status.get('risk_mode')}",
            reply_markup=self._keyboard(),
        )

    async def _boost_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self._boost_setter is None:
            await update.effective_message.reply_text("Boost control is not configured", reply_markup=self._keyboard())
            return

        desired = True
        if context.args:
            arg = context.args[0].lower()
            if arg in {"off", "0", "false"}:
                desired = False
            elif arg in {"on", "1", "true"}:
                desired = True
            else:
                await update.effective_message.reply_text("Использование: /boost on|off", reply_markup=self._keyboard())
                return

        status = await self._boost_setter(desired)
        await update.effective_message.reply_text(
            f"High-conviction boost {'ENABLED' if status.get('high_conviction_boost_enabled') else 'DISABLED'}",
            reply_markup=self._keyboard(),
        )

    async def _price_filter_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self._price_filter_setter is None:
            await update.effective_message.reply_text("Price filter control is not configured", reply_markup=self._keyboard())
            return

        if not context.args:
            await update.effective_message.reply_text("Использование: /pricefilter on|off", reply_markup=self._keyboard())
            return

        arg = context.args[0].lower()
        if arg in {"on", "1", "true"}:
            desired = True
        elif arg in {"off", "0", "false"}:
            desired = False
        else:
            await update.effective_message.reply_text("Использование: /pricefilter on|off", reply_markup=self._keyboard())
            return

        status = await self._price_filter_setter(desired)
        await update.effective_message.reply_text(
            f"Price filter {'ENABLED' if status.get('price_filter_enabled') else 'DISABLED'}",
            reply_markup=self._keyboard(),
        )

    async def _topwallets_handler(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self._top_wallets_provider is None:
            await update.effective_message.reply_text("Top wallets provider is not configured", reply_markup=self._keyboard())
            return

        rows = await self._top_wallets_provider()
        if not rows:
            await update.effective_message.reply_text("Top wallets list is empty", reply_markup=self._keyboard())
            return

        lines = ["Top discovered wallets:"]
        for idx, row in enumerate(rows, start=1):
            enabled = "ON" if row.get("enabled") else "off"
            lines.append(
                f"{idx}. {row.get('address')} | score={row.get('score', 0):.2f} | wr={row.get('win_rate', 0) * 100:.1f}% | {enabled}"
            )
        await update.effective_message.reply_text("\n".join(lines), reply_markup=self._keyboard())

    async def _add_wallet_handler_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self._add_wallet_handler is None:
            await update.effective_message.reply_text("Add wallet control is not configured", reply_markup=self._keyboard())
            return
        if not context.args:
            await update.effective_message.reply_text("Использование: /add 0x...", reply_markup=self._keyboard())
            return
        result = await self._add_wallet_handler(context.args[0])
        if result.get("ok"):
            await update.effective_message.reply_text(
                f"Wallet added: {result.get('address')}",
                reply_markup=self._keyboard(),
            )
        else:
            await update.effective_message.reply_text(
                f"Add failed: {result.get('error', 'unknown')}",
                reply_markup=self._keyboard(),
            )

    async def _remove_wallet_handler_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self._remove_wallet_handler is None:
            await update.effective_message.reply_text("Remove wallet control is not configured", reply_markup=self._keyboard())
            return
        if not context.args:
            await update.effective_message.reply_text("Использование: /remove 0x...", reply_markup=self._keyboard())
            return
        result = await self._remove_wallet_handler(context.args[0])
        if result.get("ok"):
            await update.effective_message.reply_text(
                f"Wallet removed: {result.get('address')}",
                reply_markup=self._keyboard(),
            )
        else:
            await update.effective_message.reply_text(
                f"Remove failed: {result.get('error', 'not_found')}",
                reply_markup=self._keyboard(),
            )

    async def _discovery_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self._discovery_status_provider is None:
            await update.effective_message.reply_text("Discovery status is not configured", reply_markup=self._keyboard())
            return
        if context.args and context.args[0].lower() != "status":
            await update.effective_message.reply_text("Использование: /discovery status", reply_markup=self._keyboard())
            return

        status = await self._discovery_status_provider()
        lines = [
            f"Auto-add: {'ON' if status.get('autoadd') else 'OFF'}",
            f"Last run: {status.get('last_run_at')}",
            f"Candidates: {status.get('scanned_candidates', 0)}",
            f"Passed filters: {status.get('passed_filters', 0)}",
            f"Stored top: {status.get('stored_top', 0)}",
            f"Enabled wallets: {status.get('enabled_wallets', 0)}",
            (
                "Approvals: requested="
                f"{status.get('approvals_requested', 0)} "
                f"approved={status.get('approvals_granted', 0)} "
                f"skipped={status.get('approvals_skipped', 0)}"
            ),
        ]
        await update.effective_message.reply_text("\n".join(lines), reply_markup=self._keyboard())

    async def _autoadd_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self._autoadd_setter is None:
            await update.effective_message.reply_text("Auto-add control is not configured", reply_markup=self._keyboard())
            return
        if not context.args:
            await update.effective_message.reply_text("Использование: /autoadd on|off", reply_markup=self._keyboard())
            return
        arg = context.args[0].lower()
        if arg in {"on", "1", "true"}:
            enabled = True
        elif arg in {"off", "0", "false"}:
            enabled = False
        else:
            await update.effective_message.reply_text("Использование: /autoadd on|off", reply_markup=self._keyboard())
            return
        status = await self._autoadd_setter(enabled)
        await update.effective_message.reply_text(
            f"Discovery auto-add {'ENABLED' if status.get('discovery_autoadd') else 'DISABLED'}",
            reply_markup=self._keyboard(),
        )

    async def _button_handler(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is not None and update.effective_message is not None:
            logger.info(
                "Telegram button/text received chat_id={} text={}",
                update.effective_chat.id,
                update.effective_message.text,
            )
        if not self._authorized(update):
            logger.warning(
                "Unauthorized Telegram message ignored chat_id={} expected_chat_id={}",
                update.effective_chat.id if update.effective_chat else None,
                self._chat_id,
            )
            return

        text = (update.effective_message.text or "").strip()

        if text == BTN_STATUS:
            await update.effective_message.reply_text(await self._build_status_text(), reply_markup=self._keyboard())
            return
        if text == BTN_PNL:
            await update.effective_message.reply_text(await self._build_pnl_text(), reply_markup=self._keyboard())
            return
        if text == BTN_TRADES:
            await update.effective_message.reply_text(await self._build_trades_text(limit=10), reply_markup=self._keyboard())
            return
        if text == BTN_POSITIONS:
            await update.effective_message.reply_text(
                await self._build_positions_text(open_only=True, limit=10),
                reply_markup=self._keyboard(),
            )
            return
        if text == BTN_MODE_AGGR:
            if self._mode_setter is None:
                return
            status = await self._mode_setter("aggressive")
            await update.effective_message.reply_text(
                f"Режим риска обновлён: {status.get('risk_mode')}",
                reply_markup=self._keyboard(),
            )
            return
        if text == BTN_MODE_CONS:
            if self._mode_setter is None:
                return
            status = await self._mode_setter("conservative")
            await update.effective_message.reply_text(
                f"Режим риска обновлён: {status.get('risk_mode')}",
                reply_markup=self._keyboard(),
            )
            return
        if text == BTN_BOOST_ON:
            if self._boost_setter is None:
                return
            status = await self._boost_setter(True)
            await update.effective_message.reply_text(
                f"Boost: {'ON' if status.get('high_conviction_boost_enabled') else 'OFF'}",
                reply_markup=self._keyboard(),
            )
            return
        if text == BTN_BOOST_OFF:
            if self._boost_setter is None:
                return
            status = await self._boost_setter(False)
            await update.effective_message.reply_text(
                f"Boost: {'ON' if status.get('high_conviction_boost_enabled') else 'OFF'}",
                reply_markup=self._keyboard(),
            )
            return
        if text == BTN_FILTER_ON:
            if self._price_filter_setter is None:
                return
            status = await self._price_filter_setter(True)
            await update.effective_message.reply_text(
                f"Фильтр цены: {'ON' if status.get('price_filter_enabled') else 'OFF'}",
                reply_markup=self._keyboard(),
            )
            return
        if text == BTN_FILTER_OFF:
            if self._price_filter_setter is None:
                return
            status = await self._price_filter_setter(False)
            await update.effective_message.reply_text(
                f"Фильтр цены: {'ON' if status.get('price_filter_enabled') else 'OFF'}",
                reply_markup=self._keyboard(),
            )
            return
        if text == BTN_MENU:
            await update.effective_message.reply_text("Кнопки управления ниже.", reply_markup=self._keyboard())
            return

        await update.effective_message.reply_text(
            "Не понял команду. Используйте кнопки меню ниже.",
            reply_markup=self._keyboard(),
        )

    async def _build_status_text(self) -> str:
        if self._status_provider is None:
            return "Status provider is not configured"

        status = await self._status_provider()
        params = status.get("risk_params", {})
        return (
            f"Engine: {'DRY RUN' if status.get('dry_run') else 'LIVE'}\n"
            f"Risk mode: {status.get('risk_mode')}\n"
            f"Trading: {'ENABLED' if status.get('trading_enabled', True) else 'PAUSED'}\n"
            f"Scheduler: {'running' if status.get('scheduler_running') else 'stopped'}\n"
            f"Tracked wallets: {status.get('tracked_wallets', 0)}\n"
            f"Open positions: {status.get('open_positions', 0)}\n"
            f"Daily PnL: ${status.get('daily_pnl_usd', 0.0):.2f}\n"
            f"Drawdown(24h): {status.get('daily_drawdown_pct', 0.0) * 100:.2f}%\n"
            f"Per-wallet cap: {params.get('max_per_wallet_pct', 0.0) * 100:.1f}%\n"
            f"Per-position cap: {params.get('max_per_position_pct', 0.0) * 100:.1f}%\n"
            f"Total exposure cap: {params.get('max_total_exposure_pct', 0.0) * 100:.1f}%\n"
            f"Kelly x{params.get('kelly_multiplier', 0.0):.2f}\n"
            f"Price filter: {'ON' if status.get('price_filter_enabled') else 'OFF'}\n"
            f"Boost: {'ON' if status.get('high_conviction_boost_enabled') else 'OFF'}\n"
            f"Discovery auto-add: {'ON' if status.get('discovery_autoadd') else 'OFF'}"
        )

    async def _build_pnl_text(self) -> str:
        if self._pnl_provider is None:
            return "PnL provider is not configured"

        pnl = await self._pnl_provider()
        return (
            f"Total equity: ${pnl.get('total_equity_usd', 0.0):.2f}\n"
            f"Exposure: ${pnl.get('exposure_usd', 0.0):.2f}\n"
            f"Daily PnL: ${pnl.get('daily_pnl_usd', 0.0):.2f}\n"
            f"Cumulative PnL: ${pnl.get('cumulative_pnl_usd', 0.0):.2f}"
        )

    async def _build_trades_text(self, *, limit: int = 10) -> str:
        if self._trades_provider is None:
            return "Trades provider is not configured"

        rows = await self._trades_provider(max(1, min(limit, 30)))
        if not rows:
            return "Сделок пока нет."

        lines = ["Последние сделки:"]
        for row in rows[:30]:
            status = str(row.get("status", "unknown")).upper()
            side = str(row.get("side", "")).upper()
            market = row.get("market_id", "n/a")
            outcome = row.get("outcome", "n/a")
            size = float(row.get("size_usd", 0.0) or 0.0)
            price = float(row.get("price_cents", 0.0) or 0.0)
            wallet = self._short_address(str(row.get("wallet_address", "")))
            lines.append(
                f"- [{status}] {side} {market}/{outcome} | ${size:.2f} @ {price:.1f}c | {wallet}"
            )
        return "\n".join(lines)

    async def _build_positions_text(self, *, open_only: bool, limit: int = 10) -> str:
        if self._positions_provider is None:
            return "Positions provider is not configured"

        rows = await self._positions_provider(open_only, max(1, min(limit, 30)))
        if not rows:
            return "Открытых позиций пока нет." if open_only else "Позиции пока отсутствуют."

        header = "Открытые позиции:" if open_only else "Позиции (open + closed):"
        lines = [header]
        for row in rows[:30]:
            state = "OPEN" if row.get("is_open") else "CLOSED"
            market = row.get("market_id", "n/a")
            outcome = row.get("outcome", "n/a")
            invested = float(row.get("invested_usd", 0.0) or 0.0)
            unrealized = float(row.get("unrealized_pnl_usd", 0.0) or 0.0)
            realized = float(row.get("realized_pnl_usd", 0.0) or 0.0)
            lines.append(
                f"- [{state}] {market}/{outcome} | invested ${invested:.2f} | U-PnL ${unrealized:.2f} | R-PnL ${realized:.2f}"
            )
        return "\n".join(lines)

    async def _approval_callback(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if update.callback_query is None:
            return

        action, _, request_id = (update.callback_query.data or "").partition(":")
        approval_request = self._pending_approvals.get(request_id)
        if approval_request is None:
            await update.callback_query.answer("Request expired")
            return

        is_approved = action in {"approve", "wallet_approve"}
        if not approval_request.future.done():
            approval_request.future.set_result(is_approved)

        if is_approved:
            response = "Approved"
        elif action == "reject":
            response = "Rejected"
        else:
            response = "Skipped"
        user = update.effective_user.username if update.effective_user else "unknown"
        await update.callback_query.answer(response)
        await update.callback_query.edit_message_text(f"{response} by @{user}")

    def _authorized(self, update: Update) -> bool:
        return bool(update.effective_chat and update.effective_chat.id == self._chat_id and update.effective_message)

    @staticmethod
    def _short_address(address: str) -> str:
        if len(address) < 12:
            return address
        return f"{address[:6]}...{address[-4:]}"

    @staticmethod
    def _keyboard() -> ReplyKeyboardMarkup:
        from config.settings import settings
        
        url = f"{settings.bot_public_url}/dashboard"
        if settings.dashboard_write_token:
            url += f"?token={settings.dashboard_write_token}"
            
        keyboard = [
            [KeyboardButton("🌐 Открыть Dashboard", web_app=WebAppInfo(url=url))],
            [KeyboardButton(BTN_STATUS), KeyboardButton(BTN_PNL)],
            [KeyboardButton(BTN_TRADES), KeyboardButton(BTN_POSITIONS)],
            [KeyboardButton(BTN_MODE_AGGR), KeyboardButton(BTN_MODE_CONS)],
            [KeyboardButton(BTN_BOOST_ON), KeyboardButton(BTN_BOOST_OFF)],
            [KeyboardButton(BTN_FILTER_ON), KeyboardButton(BTN_FILTER_OFF)],
            [KeyboardButton(BTN_MENU)],
        ]
        return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)
