# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""
Feishu (Lark) Channel Provider.

This module provides the channel provider for Feishu/Lark integration.
It manages the Feishu client lifecycle using Event Subscription mode.

IM channels are stored as Messager CRD in the kinds table.
"""

import asyncio
import logging
from typing import Any, Dict, Optional

from lark_oapi import Client

from app.services.channels.base import BaseChannelProvider
from app.services.channels.feishu.callback import feishu_callback_service
from app.services.channels.feishu.handler import FeishuChannelHandler

logger = logging.getLogger(__name__)

# CRD kind for IM channels
MESSAGER_KIND = "Messager"
MESSAGER_USER_ID = 0

# Message deduplication settings
FEISHU_MSG_DEDUP_PREFIX = "feishu:msg_dedup:"
FEISHU_MSG_DEDUP_TTL = 300  # 5 minutes


def _get_channel_default_team_id(channel_id: int) -> Optional[int]:
    """
    Get the current default_team_id for a channel from database.

    This function is used by the handler to dynamically get the latest
    default_team_id, allowing configuration updates without restart.

    Args:
        channel_id: The IM channel ID (Kind.id)

    Returns:
        The default team ID or None
    """
    from app.db.session import SessionLocal
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
        if channel:
            spec = channel.json.get("spec", {})
            return spec.get("defaultTeamId", 0)
        return None
    finally:
        db.close()


def _get_channel_default_model_name(channel_id: int) -> Optional[str]:
    """
    Get the current default_model_name for a channel from database.

    Args:
        channel_id: The IM channel ID (Kind.id)

    Returns:
        The default model name or None
    """
    from app.db.session import SessionLocal
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
        if channel:
            spec = channel.json.get("spec", {})
            model_name = spec.get("defaultModelName", "")
            return model_name if model_name else None
        return None
    finally:
        db.close()


def _get_channel_user_mapping_config(channel_id: int) -> Dict[str, Any]:
    """
    Get the user mapping configuration for a channel from database.

    Args:
        channel_id: The IM channel ID (Kind.id)

    Returns:
        Dict with user_mapping_mode and user_mapping_config.
    """
    from app.db.session import SessionLocal
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
        if channel:
            spec = channel.json.get("spec", {})
            config = spec.get("config", {})
            return {
                "mode": config.get("user_mapping_mode", "select_user"),
                "config": config.get("user_mapping_config"),
            }
        return {"mode": "select_user", "config": None}
    finally:
        db.close()


class FeishuChannelProvider(BaseChannelProvider):
    """
    Feishu channel provider using Event Subscription.

    Manages the Feishu client lifecycle, including:
    - Starting and stopping the client
    - Processing incoming messages
    - Reconnection logic with exponential backoff
    """

    def __init__(self, channel: Any):
        """
        Initialize the Feishu channel provider.

        Args:
            channel: Channel-like object (IMChannelAdapter) with Feishu configuration
        """
        super().__init__(channel)
        self._client: Optional[Client] = None
        self._task: Optional[asyncio.Task] = None
        self._handler: Optional[FeishuChannelHandler] = None

    @property
    def app_id(self) -> Optional[str]:
        """Get the Feishu app ID from config."""
        return self.config.get("app_id")

    @property
    def app_secret(self) -> Optional[str]:
        """Get the Feishu app secret from config."""
        return self.config.get("app_secret")

    @property
    def verification_token(self) -> Optional[str]:
        """Get the Feishu verification token from config."""
        return self.config.get("verification_token")

    @property
    def encrypt_key(self) -> Optional[str]:
        """Get the Feishu encrypt key from config."""
        return self.config.get("encrypt_key")

    def _is_configured(self) -> bool:
        """Check if Feishu is properly configured."""
        return bool(self.app_id and self.app_secret)

    async def start(self) -> bool:
        """
        Start the Feishu client.

        Returns:
            True if started successfully, False otherwise
        """
        if not self._is_configured():
            self._set_error("Feishu not configured: missing app_id or app_secret")
            return False

        if self._is_running:
            logger.warning(
                "[Feishu] Channel %s (id=%d) is already running",
                self.channel_name,
                self.channel_id,
            )
            return True

        try:
            logger.info(
                "[Feishu] Starting channel %s (id=%d)...",
                self.channel_name,
                self.channel_id,
            )

            # Create Feishu client
            self._client = (
                Client.builder().app_id(self.app_id).app_secret(self.app_secret).build()
            )

            # Create handler with dynamic configuration getters
            channel_id = self.channel_id
            self._handler = FeishuChannelHandler(
                channel_id=channel_id,
                client=self._client,
                get_default_team_id=lambda: _get_channel_default_team_id(channel_id),
                get_default_model_name=lambda: _get_channel_default_model_name(
                    channel_id
                ),
                get_user_mapping_config=lambda: _get_channel_user_mapping_config(
                    channel_id
                ),
            )

            # Register client with callback service
            feishu_callback_service.register_client(channel_id, self._client)

            # Start the event listener in background
            self._task = asyncio.create_task(self._run_event_listener())
            self._set_running(True)

            logger.info(
                "[Feishu] Channel %s (id=%d) started successfully, app_id=%s...",
                self.channel_name,
                self.channel_id,
                self.app_id[:8] if self.app_id else "N/A",
            )
            return True

        except Exception as e:
            self._set_error(f"Failed to start: {e}")
            self._set_running(False)
            return False

    async def _run_event_listener(self) -> None:
        """
        Run the event listener loop.

        This method runs the Feishu event listener in a loop,
        automatically reconnecting on disconnection or errors.
        """
        from lark.oapi import EventDispatcher

        retry_count = 0
        max_retries = 10
        base_delay = 1.0

        while self._is_running:
            try:
                logger.info(
                    "[Feishu] Channel %s (id=%d) starting event listener...",
                    self.channel_name,
                    self.channel_id,
                )

                # Create event dispatcher
                dispatcher = EventDispatcher(self._client)

                # Register message event handler
                from lark.oapi.api.im.message.event.v1 import (
                    Handler as ImMessageHandler,
                )
                from lark.oapi.api.im.message.event.v1 import (
                    ReceiveEvent as ImReceiveEvent,
                )

                # Create handler wrapper for message events
                async def on_message_received(event: ImReceiveEvent):
                    await self._handler.handle_event(event)

                dispatcher.events(ImReceiveEvent)(on_message_received)

                # Start the event dispatcher (blocking call)
                await dispatcher.start()

            except asyncio.CancelledError:
                logger.info(
                    "[Feishu] Channel %s (id=%d) task cancelled",
                    self.channel_name,
                    self.channel_id,
                )
                break

            except Exception as e:
                if not self._is_running:
                    break

                retry_count += 1
                if retry_count > max_retries:
                    self._set_error(f"Max retries ({max_retries}) exceeded")
                    self._set_running(False)
                    break

                # Exponential backoff with max 60 seconds
                delay = min(base_delay * (2 ** (retry_count - 1)), 60.0)
                logger.warning(
                    "[Feishu] Channel %s (id=%d) connection error (attempt %d/%d), "
                    "reconnecting in %.1fs: %s",
                    self.channel_name,
                    self.channel_id,
                    retry_count,
                    max_retries,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)

            else:
                # Reset retry count on successful connection
                retry_count = 0

        logger.info(
            "[Feishu] Channel %s (id=%d) event listener exited",
            self.channel_name,
            self.channel_id,
        )

    async def stop(self) -> None:
        """Stop the Feishu client."""
        if not self._is_running:
            logger.debug(
                "[Feishu] Channel %s (id=%d) is not running",
                self.channel_name,
                self.channel_id,
            )
            return

        logger.info(
            "[Feishu] Stopping channel %s (id=%d)...",
            self.channel_name,
            self.channel_id,
        )
        self._set_running(False)

        # Unregister client from callback service
        feishu_callback_service.unregister_client(self.channel_id)

        # Cancel the background task
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        self._task = None
        self._client = None
        self._handler = None

        logger.info(
            "[Feishu] Channel %s (id=%d) stopped",
            self.channel_name,
            self.channel_id,
        )

    def get_status(self) -> Dict[str, Any]:
        """
        Get the current status of the Feishu provider.

        Returns:
            Dictionary containing status information
        """
        status = super().get_status()
        status["extra_info"] = {
            "app_id_prefix": self.app_id[:8] if self.app_id else None,
            "has_verification_token": bool(self.verification_token),
            "has_encrypt_key": bool(self.encrypt_key),
        }
        return status

    async def send_text_message(
        self, receive_id: str, text: str, msg_type: str = "text"
    ) -> bool:
        """
        Send a text message to a Feishu user/chat.

        Args:
            receive_id: The receiver ID (user_id or chat_id)
            text: Message text content
            msg_type: Message type ("text" or "interactive")

        Returns:
            True if sent successfully, False otherwise
        """
        if not self._client:
            logger.error("[Feishu] Client not initialized")
            return False

        try:
            import json

            from lark.oapi.api.im.v1 import (
                CreateMessageReqBody,
                CreateMessageRequest,
            )

            content = json.dumps({"text": text} if msg_type == "text" else text)

            req = (
                CreateMessageRequest.builder()
                .request_body(
                    CreateMessageReqBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                )
                .build()
            )

            resp = await self._client.im.message.create(req)
            return resp.code == 0

        except Exception as e:
            logger.error("[Feishu] Failed to send message: %s", e)
            return False
