from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import parse_qsl
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

from config.settings import settings

RiskMode = Literal["aggressive", "conservative"]

BTN_STATUS = "📊 Статус"
BTN_PNL = "💰 PnL"
BTN_TRADES = "🧾 Сделки"
BTN_POSITIONS = "📦 Позиции"
BTN_TRADE_START = "▶️ Возобновить торговлю"
BTN_TRADE_PAUSE = "⏸ Пауза торговли"
BTN_MODE_AGGR = "⚡ Переключить в Aggressive"
BTN_MODE_CONS = "🛡 Переключить в Conservative"
BTN_BOOST_ON = "🚀 Включить буст"
BTN_BOOST_OFF = "🧯 Выключить буст"
BTN_FILTER_ON = "🎯 Включить фильтр цены"
BTN_FILTER_OFF = "🧩 Выключить фильтр цены"
BTN_MENU = "📌 Показать меню"


@dataclass(slots=True)
class ApprovalRequest:
    request_id: str
    expires_at: datetime
    future: asyncio.Future[bool]


@dataclass(slots=True)
class DashboardSession:
    token: str
    expires_at: datetime
    user_id: int
    username: str | None


class NotificationService:
    """Telegram notifier with commands + Russian keyboard controls."""

    def __init__(self, token: str | None, chat_id: int | None, alert_chat_id: int | None = None) -> None:
        self._token = token
        self._control_chat_id = chat_id
        self._alert_chat_id = alert_chat_id or chat_id
        self._application: Application | None = None
        self._pending_approvals: dict[str, ApprovalRequest] = {}
        self._dashboard_sessions: dict[str, DashboardSession] = {}
        self._status_provider: Callable[[], Awaitable[dict]] | None = None
        self._pnl_provider: Callable[[], Awaitable[dict]] | None = None
        self._mode_setter: Callable[[RiskMode], Awaitable[dict]] | None = None
        self._trading_setter: Callable[[bool], Awaitable[dict]] | None = None
        self._boost_setter: Callable[[bool], Awaitable[dict]] | None = None
        self._price_filter_setter: Callable[[bool], Awaitable[dict]] | None = None
        self._top_wallets_provider: Callable[[], Awaitable[list[dict]]] | None = None
        self._add_wallet_handler: Callable[[str], Awaitable[dict]] | None = None
        self._remove_wallet_handler: Callable[[str], Awaitable[dict]] | None = None
        self._discovery_status_provider: Callable[[], Awaitable[dict]] | None = None
        self._autoadd_setter: Callable[[bool], Awaitable[dict]] | None = None
        self._trades_provider: Callable[[int], Awaitable[list[dict]]] | None = None
        self._positions_provider: Callable[[bool, int], Awaitable[list[dict]]] | None = None
        self._status_snapshot: dict[str, object] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._control_chat_id)

    def register_status_provider(self, provider: Callable[[], Awaitable[dict]]) -> None:
        self._status_provider = provider

    def register_pnl_provider(self, provider: Callable[[], Awaitable[dict]]) -> None:
        self._pnl_provider = provider

    def register_controls(
        self,
        *,
        mode_setter: Callable[[RiskMode], Awaitable[dict]],
        trading_setter: Callable[[bool], Awaitable[dict]] | None = None,
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
        self._trading_setter = trading_setter
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
            await self._sync_status_snapshot()
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
            self._dashboard_sessions.clear()

    async def send_message(self, message: str, *, parse_mode: str | None = None) -> None:
        if self._application is None or not self.enabled:
            logger.debug("Telegram message skipped (service disabled): {}", message)
            return

        await self._application.bot.send_message(
            chat_id=self._alert_chat_id,
            text=message,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )

    async def send_control_panel_message(self, text: str) -> None:
        if self._application is None or not self.enabled:
            return
        await self._sync_status_snapshot()
        await self._application.bot.send_message(
            chat_id=self._control_chat_id,
            text=text,
            reply_markup=self._keyboard(),
        )

    async def _reply(
        self,
        update: Update,
        text: str,
        *,
        parse_mode: str | None = None,
        refresh_status: bool = False,
    ) -> None:
        if update.effective_message is None:
            return
        if refresh_status:
            await self._sync_status_snapshot()
        await update.effective_message.reply_text(
            text,
            reply_markup=self._keyboard(),
            parse_mode=parse_mode,
            disable_web_page_preview=True,
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
            chat_id=self._control_chat_id,
            text=f"{html.escape(title)}\n\n{html.escape(body)}\n\nExpires in {timeout_seconds} sec.",
            reply_markup=keyboard,
            parse_mode="HTML",
            disable_web_page_preview=True,
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
            chat_id=self._control_chat_id,
            text=f"{html.escape(title)}\n\n{html.escape(body)}\n\nExpires in {timeout_seconds} sec.",
            reply_markup=keyboard,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError:
            logger.warning("Wallet approval request {} expired", request_id)
            return False
        finally:
            self._pending_approvals.pop(request_id, None)

    def issue_dashboard_session(self, init_data: str) -> dict[str, str | int]:
        payload = self._verify_telegram_webapp_init_data(init_data)
        user = payload.get("user", {})
        user_id = int(user.get("id", 0))
        username = self._normalize_username(user.get("username"))
        expires_at = datetime.now(tz=timezone.utc) + timedelta(
            seconds=max(60, settings.telegram_webapp_session_ttl_seconds)
        )
        token = secrets.token_urlsafe(32)
        self._dashboard_sessions[token] = DashboardSession(
            token=token,
            expires_at=expires_at,
            user_id=user_id,
            username=username,
        )
        self._prune_dashboard_sessions()
        return {
            "dashboard_token": token,
            "expires_at": expires_at.isoformat(),
            "user_id": user_id,
        }

    def has_valid_dashboard_session(self, token: str | None) -> bool:
        if not token:
            return False
        self._prune_dashboard_sessions()
        session = self._dashboard_sessions.get(token)
        if session is None:
            return False
        if session.expires_at <= datetime.now(tz=timezone.utc):
            self._dashboard_sessions.pop(token, None)
            return False
        return True

    async def _sync_status_snapshot(self) -> None:
        if self._status_provider is None:
            return
        try:
            self._store_status_snapshot(await self._status_provider())
        except Exception:
            logger.exception("Failed to refresh Telegram status snapshot")

    def _store_status_snapshot(self, status: dict | None) -> None:
        if status:
            self._status_snapshot = dict(status)

    def _prune_dashboard_sessions(self) -> None:
        now = datetime.now(tz=timezone.utc)
        expired_tokens = [token for token, session in self._dashboard_sessions.items() if session.expires_at <= now]
        for token in expired_tokens:
            self._dashboard_sessions.pop(token, None)

    def _verify_telegram_webapp_init_data(self, init_data: str) -> dict[str, object]:
        if not self._token:
            raise ValueError("telegram_not_configured")

        items = dict(parse_qsl(init_data, keep_blank_values=True))
        provided_hash = items.pop("hash", None)
        if not provided_hash:
            raise ValueError("telegram_hash_missing")

        data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(items.items()))
        secret_key = hmac.new(b"WebAppData", self._token.encode("utf-8"), hashlib.sha256).digest()
        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(calculated_hash, provided_hash):
            raise ValueError("telegram_hash_invalid")

        auth_date = int(items.get("auth_date", "0") or 0)
        now_ts = int(datetime.now(tz=timezone.utc).timestamp())
        max_age = max(300, settings.telegram_webapp_session_ttl_seconds * 2)
        if auth_date <= 0 or now_ts - auth_date > max_age:
            raise ValueError("telegram_auth_expired")

        try:
            user = json.loads(items.get("user", "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("telegram_user_invalid") from exc

        user_id = int(user.get("id", 0) or 0)
        username = self._normalize_username(user.get("username"))
        if not self._is_user_identity_allowed(user_id=user_id, username=username):
            raise PermissionError("telegram_user_not_allowed")

        return {"user": user, "auth_date": auth_date}

    @staticmethod
    def _normalize_username(username: object) -> str | None:
        if username is None:
            return None
        cleaned = str(username).strip().lstrip("@").lower()
        return cleaned or None

    def _is_user_identity_allowed(self, *, user_id: int | None, username: str | None) -> bool:
        allowed_ids = set(settings.telegram_allowed_user_ids)
        allowed_usernames = set(settings.telegram_allowed_usernames)
        if not allowed_ids and not allowed_usernames:
            return True
        if user_id is not None and user_id in allowed_ids:
            return True
        if username and username in allowed_usernames:
            return True
        return False

    async def _status_handler(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await self._reply(update, await self._build_status_text(), parse_mode="HTML", refresh_status=True)

    async def _pnl_handler(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await self._reply(update, await self._build_pnl_text(), parse_mode="HTML")

    async def _trades_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        limit = 10
        if context.args:
            try:
                limit = int(context.args[0])
            except ValueError:
                await self._reply(update, "Использование: /trades [limit]")
                return
        await self._reply(update, await self._build_trades_text(limit=limit), parse_mode="HTML")

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
                        await self._reply(update, "Использование: /positions [open|all] [limit]")
                        return
            else:
                try:
                    limit = int(arg0)
                except ValueError:
                    await self._reply(update, "Использование: /positions [open|all] [limit]")
                    return

        await self._reply(
            update,
            await self._build_positions_text(open_only=open_only, limit=limit),
            parse_mode="HTML",
        )

    async def _menu_handler(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await self._reply(update, "Кнопки управления ниже.", refresh_status=True)

    async def _mode_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self._mode_setter is None:
            await self._reply(update, "Mode control is not configured")
            return

        if not context.args or context.args[0].lower() not in {"aggressive", "conservative"}:
            await self._reply(update, "Использование: /mode aggressive|conservative")
            return

        mode = context.args[0].lower()
        status = await self._mode_setter(mode)  # type: ignore[arg-type]
        self._store_status_snapshot(status)
        await self._reply(update, f"Режим риска обновлён: {status.get('risk_mode')}")

    async def _boost_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self._boost_setter is None:
            await self._reply(update, "Boost control is not configured")
            return

        desired = True
        if context.args:
            arg = context.args[0].lower()
            if arg in {"off", "0", "false"}:
                desired = False
            elif arg in {"on", "1", "true"}:
                desired = True
            else:
                await self._reply(update, "Использование: /boost on|off")
                return

        status = await self._boost_setter(desired)
        self._store_status_snapshot(status)
        await self._reply(update, f"High-conviction boost {'ENABLED' if status.get('high_conviction_boost_enabled') else 'DISABLED'}")

    async def _price_filter_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self._price_filter_setter is None:
            await self._reply(update, "Price filter control is not configured")
            return

        if not context.args:
            await self._reply(update, "Использование: /pricefilter on|off")
            return

        arg = context.args[0].lower()
        if arg in {"on", "1", "true"}:
            desired = True
        elif arg in {"off", "0", "false"}:
            desired = False
        else:
            await self._reply(update, "Использование: /pricefilter on|off")
            return

        status = await self._price_filter_setter(desired)
        self._store_status_snapshot(status)
        await self._reply(update, f"Price filter {'ENABLED' if status.get('price_filter_enabled') else 'DISABLED'}")

    async def _topwallets_handler(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self._top_wallets_provider is None:
            await self._reply(update, "Top wallets provider is not configured")
            return

        rows = await self._top_wallets_provider()
        if not rows:
            await self._reply(update, "Top wallets list is empty")
            return

        lines = ["Top discovered wallets:"]
        for idx, row in enumerate(rows, start=1):
            enabled = "ON" if row.get("enabled") else "off"
            lines.append(
                f"{idx}. {row.get('address')} | score={row.get('score', 0):.2f} | wr={row.get('win_rate', 0) * 100:.1f}% | {enabled}"
            )
        await self._reply(update, "\n".join(lines))

    async def _add_wallet_handler_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self._add_wallet_handler is None:
            await self._reply(update, "Add wallet control is not configured")
            return
        if not context.args:
            await self._reply(update, "Использование: /add 0x...")
            return
        result = await self._add_wallet_handler(context.args[0])
        if result.get("ok"):
            await self._reply(update, f"Wallet added: {result.get('address')}")
        else:
            await self._reply(update, f"Add failed: {result.get('error', 'unknown')}")

    async def _remove_wallet_handler_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self._remove_wallet_handler is None:
            await self._reply(update, "Remove wallet control is not configured")
            return
        if not context.args:
            await self._reply(update, "Использование: /remove 0x...")
            return
        result = await self._remove_wallet_handler(context.args[0])
        if result.get("ok"):
            await self._reply(update, f"Wallet removed: {result.get('address')}")
        else:
            await self._reply(update, f"Remove failed: {result.get('error', 'not_found')}")

    async def _discovery_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self._discovery_status_provider is None:
            await self._reply(update, "Discovery status is not configured")
            return
        if context.args and context.args[0].lower() != "status":
            await self._reply(update, "Использование: /discovery status")
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
        await self._reply(update, "\n".join(lines))

    async def _autoadd_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self._autoadd_setter is None:
            await self._reply(update, "Auto-add control is not configured")
            return
        if not context.args:
            await self._reply(update, "Использование: /autoadd on|off")
            return
        arg = context.args[0].lower()
        if arg in {"on", "1", "true"}:
            enabled = True
        elif arg in {"off", "0", "false"}:
            enabled = False
        else:
            await self._reply(update, "Использование: /autoadd on|off")
            return
        status = await self._autoadd_setter(enabled)
        self._store_status_snapshot(status)
        await self._reply(update, f"Discovery auto-add {'ENABLED' if status.get('discovery_autoadd') else 'DISABLED'}")

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
                self._control_chat_id,
            )
            return

        text = (update.effective_message.text or "").strip()

        if text == BTN_STATUS:
            await self._reply(update, await self._build_status_text(), parse_mode="HTML", refresh_status=True)
            return
        if text == BTN_PNL:
            await self._reply(update, await self._build_pnl_text(), parse_mode="HTML")
            return
        if text == BTN_TRADES:
            await self._reply(update, await self._build_trades_text(limit=10), parse_mode="HTML")
            return
        if text == BTN_POSITIONS:
            await self._reply(update, await self._build_positions_text(open_only=True, limit=10), parse_mode="HTML")
            return
        if text == BTN_TRADE_START:
            if self._trading_setter is None:
                return
            status = await self._trading_setter(True)
            self._store_status_snapshot(status)
            await self._reply(update, "Торговля включена", refresh_status=True)
            return
        if text == BTN_TRADE_PAUSE:
            if self._trading_setter is None:
                return
            status = await self._trading_setter(False)
            self._store_status_snapshot(status)
            await self._reply(update, "Торговля поставлена на паузу", refresh_status=True)
            return
        if text in {BTN_MODE_AGGR, "⚡ Режим: Агрессивный"}:
            if self._mode_setter is None:
                return
            status = await self._mode_setter("aggressive")
            self._store_status_snapshot(status)
            await self._reply(update, f"Режим риска обновлён: {status.get('risk_mode')}", refresh_status=True)
            return
        if text in {BTN_MODE_CONS, "🛡 Режим: Консервативный"}:
            if self._mode_setter is None:
                return
            status = await self._mode_setter("conservative")
            self._store_status_snapshot(status)
            await self._reply(update, f"Режим риска обновлён: {status.get('risk_mode')}", refresh_status=True)
            return
        if text in {BTN_BOOST_ON, "🚀 Буст: Вкл"}:
            if self._boost_setter is None:
                return
            status = await self._boost_setter(True)
            self._store_status_snapshot(status)
            await self._reply(update, f"Boost: {'ON' if status.get('high_conviction_boost_enabled') else 'OFF'}", refresh_status=True)
            return
        if text in {BTN_BOOST_OFF, "🧯 Буст: Выкл"}:
            if self._boost_setter is None:
                return
            status = await self._boost_setter(False)
            self._store_status_snapshot(status)
            await self._reply(update, f"Boost: {'ON' if status.get('high_conviction_boost_enabled') else 'OFF'}", refresh_status=True)
            return
        if text in {BTN_FILTER_ON, "🎯 Фильтр цены: Вкл"}:
            if self._price_filter_setter is None:
                return
            status = await self._price_filter_setter(True)
            self._store_status_snapshot(status)
            await self._reply(update, f"Фильтр цены: {'ON' if status.get('price_filter_enabled') else 'OFF'}", refresh_status=True)
            return
        if text in {BTN_FILTER_OFF, "🧩 Фильтр цены: Выкл"}:
            if self._price_filter_setter is None:
                return
            status = await self._price_filter_setter(False)
            self._store_status_snapshot(status)
            await self._reply(update, f"Фильтр цены: {'ON' if status.get('price_filter_enabled') else 'OFF'}", refresh_status=True)
            return
        if text == BTN_MENU:
            await self._reply(update, "Кнопки управления ниже.", refresh_status=True)
            return

        await self._reply(update, "Не понял команду. Используйте кнопки меню ниже.", refresh_status=True)

    async def _build_status_text(self) -> str:
        if self._status_provider is None:
            return "Status provider is not configured"

        status = await self._status_provider()
        self._store_status_snapshot(status)
        params = status.get("risk_params", {})
        return (
            "<b>Статус бота</b>\n"
            f"• Engine: <b>{'DRY RUN' if status.get('dry_run') else 'LIVE'}</b>\n"
            f"• Risk mode: <b>{html.escape(str(status.get('risk_mode')))}</b>\n"
            f"• Trading: <b>{'ENABLED' if status.get('trading_enabled', True) else 'PAUSED'}</b>\n"
            f"• Scheduler: {'running' if status.get('scheduler_running') else 'stopped'}\n"
            f"• Tracked wallets: {int(status.get('tracked_wallets', 0) or 0)}\n"
            f"• Open positions: {int(status.get('open_positions', 0) or 0)}\n"
            f"• Daily PnL: <b>${float(status.get('daily_pnl_usd', 0.0) or 0.0):.2f}</b>\n"
            f"• Drawdown(24h): {float(status.get('daily_drawdown_pct', 0.0) or 0.0) * 100:.2f}%\n"
            f"• Per-wallet cap: {float(params.get('max_per_wallet_pct', 0.0) or 0.0) * 100:.1f}%\n"
            f"• Per-position cap: {float(params.get('max_per_position_pct', 0.0) or 0.0) * 100:.1f}%\n"
            f"• Total exposure cap: {float(params.get('max_total_exposure_pct', 0.0) or 0.0) * 100:.1f}%\n"
            f"• Kelly x{float(params.get('kelly_multiplier', 0.0) or 0.0):.2f}\n"
            f"• Price filter: {'ON' if status.get('price_filter_enabled') else 'OFF'}\n"
            f"• Boost: {'ON' if status.get('high_conviction_boost_enabled') else 'OFF'}\n"
            f"• Discovery auto-add: {'ON' if status.get('discovery_autoadd') else 'OFF'}"
        )

    async def _build_pnl_text(self) -> str:
        if self._pnl_provider is None:
            return "PnL provider is not configured"

        pnl = await self._pnl_provider()
        return (
            "<b>PnL</b>\n"
            f"• Total equity: <b>${float(pnl.get('total_equity_usd', 0.0) or 0.0):.2f}</b>\n"
            f"• Exposure: ${float(pnl.get('exposure_usd', 0.0) or 0.0):.2f}\n"
            f"• Daily PnL: ${float(pnl.get('daily_pnl_usd', 0.0) or 0.0):.2f}\n"
            f"• Cumulative PnL: ${float(pnl.get('cumulative_pnl_usd', 0.0) or 0.0):.2f}"
        )

    async def _build_trades_text(self, *, limit: int = 10) -> str:
        if self._trades_provider is None:
            return "Trades provider is not configured"

        rows = await self._trades_provider(max(1, min(limit, 30)))
        if not rows:
            return "Сделок пока нет."

        lines = ["<b>Последние сделки</b>"]
        for row in rows[:30]:
            status = str(row.get("status", "unknown")).upper()
            side = str(row.get("side", "")).upper()
            market = str(row.get("market_title") or row.get("market_id") or "n/a")
            outcome = row.get("outcome", "n/a")
            size = float(row.get("size_usd", 0.0) or 0.0)
            price = float(row.get("price_cents", 0.0) or 0.0)
            wallet = self._short_address(str(row.get("wallet_address", "")))
            reason = self._short_reason(str(row.get("reason") or ""))
            lines.append(f"• <b>{status}</b> {side} {html.escape(str(outcome))} | ${size:.2f} @ {price:.1f}c")
            lines.append(f"  {html.escape(market)}")
            lines.append(f"  wallet {html.escape(wallet)}")
            if reason:
                lines.append(f"  reason: <code>{html.escape(reason)}</code>")
        return "\n".join(lines)

    async def _build_positions_text(self, *, open_only: bool, limit: int = 10) -> str:
        if self._positions_provider is None:
            return "Positions provider is not configured"

        rows = await self._positions_provider(open_only, max(1, min(limit, 30)))
        if not rows:
            return "Открытых позиций пока нет." if open_only else "Позиции пока отсутствуют."

        header = "<b>Открытые позиции</b>" if open_only else "<b>Позиции (open + closed)</b>"
        lines = [header]
        for row in rows[:30]:
            state = "OPEN" if row.get("is_open") else "CLOSED"
            market = str(row.get("market_title") or row.get("market_id") or "n/a")
            outcome = row.get("outcome", "n/a")
            invested = float(row.get("invested_usd", 0.0) or 0.0)
            unrealized = float(row.get("unrealized_pnl_usd", 0.0) or 0.0)
            realized = float(row.get("realized_pnl_usd", 0.0) or 0.0)
            qty = float(row.get("quantity", 0.0) or 0.0)
            avg_price = float(row.get("avg_price_cents", 0.0) or 0.0)
            current_price = float(row.get("current_price_cents", 0.0) or 0.0)
            lines.append(f"• <b>{state}</b> {html.escape(str(outcome))} | qty {qty:.2f} | avg {avg_price:.1f}c | cur {current_price:.1f}c")
            lines.append(f"  {html.escape(market)}")
            lines.append(f"  invested ${invested:.2f} | U-PnL ${unrealized:.2f} | R-PnL ${realized:.2f}")
        return "\n".join(lines)

    async def _approval_callback(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if update.callback_query is None:
            return
        if not self._authorized(update):
            await update.callback_query.answer("Unauthorized", show_alert=True)
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
        user = self._normalize_username(update.effective_user.username if update.effective_user else None) or "unknown"
        original_text = update.callback_query.message.text if update.callback_query.message else "Request"
        await update.callback_query.answer(response)
        await update.callback_query.edit_message_text(
            f"{html.escape(original_text)}\n\n<b>{response}</b> by @{html.escape(user)}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    def _authorized(self, update: Update) -> bool:
        if not update.effective_chat or update.effective_chat.id != self._control_chat_id:
            return False
        user = update.effective_user
        user_id = user.id if user is not None else None
        username = self._normalize_username(user.username if user is not None else None)
        return bool(update.effective_message and self._is_user_identity_allowed(user_id=user_id, username=username))

    @staticmethod
    def _short_address(address: str) -> str:
        if len(address) < 12:
            return address
        return f"{address[:6]}...{address[-4:]}"

    @staticmethod
    def _short_reason(reason: str, limit: int = 90) -> str:
        cleaned = " ".join(reason.split())
        if len(cleaned) <= limit:
            return cleaned
        return f"{cleaned[: limit - 3]}..."

    def _keyboard(self) -> ReplyKeyboardMarkup:
        url = f"{settings.bot_public_url}/dashboard"
        trading_enabled = bool(self._status_snapshot.get("trading_enabled", True))
        risk_mode = str(self._status_snapshot.get("risk_mode", "aggressive"))
        boost_enabled = bool(self._status_snapshot.get("high_conviction_boost_enabled", True))
        price_filter_enabled = bool(self._status_snapshot.get("price_filter_enabled", False))

        trading_button = BTN_TRADE_PAUSE if trading_enabled else BTN_TRADE_START
        mode_button = BTN_MODE_CONS if risk_mode == "aggressive" else BTN_MODE_AGGR
        boost_button = BTN_BOOST_OFF if boost_enabled else BTN_BOOST_ON
        filter_button = BTN_FILTER_OFF if price_filter_enabled else BTN_FILTER_ON

        keyboard = [
            [KeyboardButton("🌐 Открыть Dashboard", web_app=WebAppInfo(url=url))],
            [KeyboardButton(BTN_STATUS), KeyboardButton(BTN_PNL)],
            [KeyboardButton(BTN_TRADES), KeyboardButton(BTN_POSITIONS)],
            [KeyboardButton(trading_button), KeyboardButton(mode_button)],
            [KeyboardButton(boost_button), KeyboardButton(filter_button)],
            [KeyboardButton(BTN_MENU)],
        ]
        return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)
