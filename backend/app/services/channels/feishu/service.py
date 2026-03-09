# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""Feishu long-connection channel provider."""

import asyncio
import importlib.util
import logging
import sys
from typing import TYPE_CHECKING, Any, Dict, Optional

from app.core.cache import cache_manager
from app.db.session import SessionLocal
from app.services.channels.base import BaseChannelProvider
from app.services.channels.feishu.handler import FeishuChannelHandler
from app.services.channels.feishu.sender import FeishuBotSender

if TYPE_CHECKING:
    from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
    from lark_oapi.ws.client import Client as FeishuWsClient

logger = logging.getLogger(__name__)

MESSAGER_KIND = "Messager"
MESSAGER_USER_ID = 0
FEISHU_MSG_DEDUP_PREFIX = "feishu:msg_dedup:"
FEISHU_MSG_DEDUP_TTL = 300
FEISHU_MAX_RETRIES = 10
FEISHU_RETRY_BASE_DELAY = 1.0
FEISHU_RETRY_MAX_DELAY = 60.0


def _get_channel_default_team_id(channel_id: int) -> Optional[int]:
    from app.models.kind import Kind

    db = SessionLocal()
    try:
        channel = (
            db.query(Kind)
            .filter(
                Kind.id == channel_id,
                Kind.kind == MESSAGER_KIND,
                Kind.user_id == MESSAGER_USER_ID,
                Kind.is_active == True,
            )
            .first()
        )
        return channel.json.get("spec", {}).get("defaultTeamId", 0) if channel else None
    finally:
        db.close()


def _get_channel_default_model_name(channel_id: int) -> Optional[str]:
    from app.models.kind import Kind

    db = SessionLocal()
    try:
        channel = (
            db.query(Kind)
            .filter(
                Kind.id == channel_id,
                Kind.kind == MESSAGER_KIND,
                Kind.user_id == MESSAGER_USER_ID,
                Kind.is_active == True,
            )
            .first()
        )
        if not channel:
            return None
        name = channel.json.get("spec", {}).get("defaultModelName", "")
        return name if name else None
    finally:
        db.close()


def _get_channel_user_mapping_config(channel_id: int) -> Dict[str, Any]:
    from app.models.kind import Kind

    db = SessionLocal()
    try:
        channel = (
            db.query(Kind)
            .filter(
                Kind.id == channel_id,
                Kind.kind == MESSAGER_KIND,
                Kind.user_id == MESSAGER_USER_ID,
                Kind.is_active == True,
            )
            .first()
        )
        if not channel:
            return {"mode": "select_user", "config": None}

        config = channel.json.get("spec", {}).get("config", {})
        return {
            "mode": config.get("user_mapping_mode", "select_user"),
            "config": config.get("user_mapping_config"),
        }
    finally:
        db.close()


class FeishuChannelProvider(BaseChannelProvider):
    """Feishu channel provider based on Feishu official long-connection SDK."""

    def __init__(self, channel: Any):
        super().__init__(channel)
        self._handler: Optional[FeishuChannelHandler] = None
        self.sender: Optional[FeishuBotSender] = None
        self._client: Optional["FeishuWsClient"] = None
        self._task: Optional[asyncio.Task] = None
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def app_id(self) -> Optional[str]:
        return self.config.get("app_id")

    @property
    def app_secret(self) -> Optional[str]:
        return self.config.get("app_secret")

    def _is_configured(self) -> bool:
        return bool(self.app_id and self.app_secret)

    async def _run_client(self) -> None:
        """Run Feishu websocket client in worker thread."""
        retry_count = 0

        while self.is_running:
            try:
                await asyncio.to_thread(self._start_client_blocking)

                if not self.is_running:
                    break

                retry_count += 1
                if retry_count > FEISHU_MAX_RETRIES:
                    self._set_error(
                        f"Long connection exited unexpectedly and exceeded max retries ({FEISHU_MAX_RETRIES})"
                    )
                    self._set_running(False)
                    break

                delay = min(
                    FEISHU_RETRY_BASE_DELAY * (2 ** (retry_count - 1)),
                    FEISHU_RETRY_MAX_DELAY,
                )
                logger.warning(
                    "[Feishu] Channel %s (id=%d) long connection exited unexpectedly "
                    "(attempt %d/%d), reconnecting in %.1fs",
                    self.channel_name,
                    self.channel_id,
                    retry_count,
                    FEISHU_MAX_RETRIES,
                    delay,
                )
                await asyncio.sleep(delay)

            except asyncio.CancelledError:
                logger.info(
                    "[Feishu] Channel %s (id=%d) worker cancelled",
                    self.channel_name,
                    self.channel_id,
                )
                raise
            except Exception as exc:
                if not self.is_running:
                    break

                retry_count += 1
                if retry_count > FEISHU_MAX_RETRIES:
                    self._set_error(
                        f"Long connection failed and exceeded max retries ({FEISHU_MAX_RETRIES}): {exc}"
                    )
                    self._set_running(False)
                    logger.exception(
                        "[Feishu] Channel %s (id=%d) long connection failed: %s",
                        self.channel_name,
                        self.channel_id,
                        exc,
                    )
                    break

                delay = min(
                    FEISHU_RETRY_BASE_DELAY * (2 ** (retry_count - 1)),
                    FEISHU_RETRY_MAX_DELAY,
                )
                logger.warning(
                    "[Feishu] Channel %s (id=%d) long connection error "
                    "(attempt %d/%d), reconnecting in %.1fs: %s",
                    self.channel_name,
                    self.channel_id,
                    retry_count,
                    FEISHU_MAX_RETRIES,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

    def _start_client_blocking(self) -> None:
        """Start Feishu websocket client in a dedicated worker thread."""
        worker_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(worker_loop)
        try:
            from lark_oapi.ws.client import Client as FeishuWsClient

            self._client = FeishuWsClient(
                app_id=self.app_id,
                app_secret=self.app_secret,
                event_handler=self._create_event_handler(),
            )
            self._client.start()
        finally:
            self._client = None
            asyncio.set_event_loop(None)
            worker_loop.close()

    async def _handle_long_connection_event(
        self, event: "P2ImMessageReceiveV1"
    ) -> None:
        """Process Feishu IM message event from websocket SDK."""
        if not self._handler:
            return

        header = getattr(event, "header", None)
        event_id = getattr(header, "event_id", "") if header else ""
        if event_id:
            dedup_key = f"{FEISHU_MSG_DEDUP_PREFIX}{event_id}"
            exists = await cache_manager.get(dedup_key)
            if exists:
                return
            await cache_manager.set(dedup_key, "1", expire=FEISHU_MSG_DEDUP_TTL)

        event_data = getattr(event, "event", None)
        message = getattr(event_data, "message", None)
        if not message or getattr(message, "message_type", "") != "text":
            return

        sender = getattr(event_data, "sender", None)
        sender_id = getattr(sender, "sender_id", None)
        payload = {
            "header": {
                "event_type": getattr(header, "event_type", "im.message.receive_v1"),
                "event_id": event_id,
            },
            "event": {
                "message": {
                    "message_id": getattr(message, "message_id", ""),
                    "chat_id": getattr(message, "chat_id", ""),
                    "chat_type": getattr(message, "chat_type", ""),
                    "message_type": getattr(message, "message_type", ""),
                    "content": getattr(message, "content", ""),
                },
                "sender": {
                    "sender_type": getattr(sender, "sender_type", ""),
                    "sender_id": {
                        "open_id": getattr(sender_id, "open_id", ""),
                        "user_id": getattr(sender_id, "user_id", ""),
                        "union_id": getattr(sender_id, "union_id", ""),
                    },
                },
                "mentions": [
                    {
                        "key": getattr(mention, "key", ""),
                    }
                    for mention in (getattr(message, "mentions", []) or [])
                ],
            },
        }
        await self._handler.handle_message(payload)

    def _create_event_handler(self) -> Any:
        """Create Feishu SDK dispatcher for long-connection events."""
        try:
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
        except Exception as exc:
            raise RuntimeError(
                "Feishu SDK dependency missing. Please install lark-oapi."
            ) from exc

        builder = EventDispatcherHandler.builder("", "")

        def _attach_error_logger(async_result: Any) -> None:
            try:
                async_result.result()
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.exception(
                    "[Feishu] Failed to process long-connection event: %s",
                    exc,
                )

        def _sync_handler(event: Any) -> None:
            coroutine = self._handle_long_connection_event(event)
            loop = self._event_loop
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None

            if loop and not loop.is_closed() and current_loop is loop:
                task = current_loop.create_task(coroutine)
                task.add_done_callback(_attach_error_logger)
                return

            if loop and not loop.is_closed():
                future = asyncio.run_coroutine_threadsafe(
                    coroutine,
                    loop,
                )
                future.add_done_callback(_attach_error_logger)
                return

            if current_loop and not current_loop.is_closed():
                task = current_loop.create_task(coroutine)
                task.add_done_callback(_attach_error_logger)
                return

            logger.warning(
                "[Feishu] Event loop unavailable, fallback to synchronous processing for channel_id=%d",
                self.channel_id,
            )
            try:
                asyncio.run(coroutine)
            except Exception as exc:
                logger.exception(
                    "[Feishu] Failed to process event in synchronous fallback: %s",
                    exc,
                )

        return builder.register_p2_im_message_receive_v1(_sync_handler).build()

    async def start(self) -> bool:
        if not self._is_configured():
            self._set_error("Feishu not configured: missing app_id or app_secret")
            return False

        try:
            sdk_available = importlib.util.find_spec("lark_oapi.ws.client") is not None
        except (ImportError, ValueError):
            sdk_available = "lark_oapi.ws.client" in sys.modules
        if not sdk_available:
            self._set_error("Feishu SDK dependency missing. Please install lark-oapi.")
            return False

        channel_id = self.channel_id
        try:
            self.sender = FeishuBotSender(self.app_id, self.app_secret)
            self._handler = FeishuChannelHandler(
                channel_id=channel_id,
                sender=self.sender,
                get_default_team_id=lambda: _get_channel_default_team_id(channel_id),
                get_default_model_name=lambda: _get_channel_default_model_name(
                    channel_id
                ),
                get_user_mapping_config=lambda: _get_channel_user_mapping_config(
                    channel_id
                ),
            )
            self._event_loop = asyncio.get_running_loop()
            self._task = asyncio.create_task(self._run_client())

            self._set_running(True)
            logger.info(
                "[Feishu] Channel %s (id=%d) started with long connection",
                self.channel_name,
                self.channel_id,
            )
            return True
        except Exception as exc:
            self._set_error(f"Failed to start Feishu long connection: {exc}")
            self._set_running(False)
            self._handler = None
            self.sender = None
            self._client = None
            self._task = None
            logger.exception(
                "[Feishu] Failed to start channel %s (id=%d): %s",
                self.channel_name,
                self.channel_id,
                exc,
            )
            return False

    async def stop(self) -> None:
        self._set_running(False)

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        self._task = None
        self._client = None
        self._event_loop = None
        self._handler = None
        self.sender = None
