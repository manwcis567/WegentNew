# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""
Feishu (Lark) Long Connection Channel Provider.

This module provides the channel provider for Feishu using the official
lark-oapi SDK's WebSocket long connection mode (lark.ws.Client).

No public IP or domain required - the SDK handles WebSocket connection
to Feishu Open Platform automatically.

IM channels are stored as Messager CRD in the kinds table.
"""

import asyncio
import logging
import threading
from typing import Any, Dict, Optional

from app.services.channels.base import BaseChannelProvider

logger = logging.getLogger(__name__)

# CRD kind for IM channels
MESSAGER_KIND = "Messager"
MESSAGER_USER_ID = 0


def _get_channel_default_team_id(channel_id: int) -> Optional[int]:
    """
    Get the current default_team_id for a channel from database.

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
    Feishu Long Connection channel provider.

    Manages the Feishu WebSocket client lifecycle using lark-oapi SDK:
    - Starting and stopping the ws client
    - Processing incoming messages via EventDispatcherHandler
    - Automatic reconnection (handled by the SDK)
    """

    def __init__(self, channel: Any):
        """
        Initialize the Feishu channel provider.

        Args:
            channel: Channel-like object (IMChannelAdapter) with Feishu configuration
        """
        super().__init__(channel)
        self._ws_client = None
        self._ws_thread: Optional[threading.Thread] = None
        self._handler = None

    @property
    def app_id(self) -> Optional[str]:
        """Get the Feishu app ID from config (stored as client_id)."""
        return self.config.get("client_id")

    @property
    def app_secret(self) -> Optional[str]:
        """Get the Feishu app secret from config (stored as client_secret)."""
        return self.config.get("client_secret")

    def _is_configured(self) -> bool:
        """Check if Feishu is properly configured."""
        return bool(self.app_id and self.app_secret)

    async def start(self) -> bool:
        """
        Start the Feishu WebSocket long connection client.

        Returns:
            True if started successfully, False otherwise
        """
        if not self._is_configured():
            self._set_error(
                "Feishu not configured: missing app_id (client_id) or app_secret (client_secret)"
            )
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

            import lark_oapi as lark

            # Create the channel handler for processing messages
            from app.services.channels.feishu.handler import FeishuChannelHandler

            channel_id = self.channel_id
            self._handler = FeishuChannelHandler(
                channel_id=channel_id,
                app_id=self.app_id,
                app_secret=self.app_secret,
                get_default_team_id=lambda: _get_channel_default_team_id(channel_id),
                get_default_model_name=lambda: _get_channel_default_model_name(
                    channel_id
                ),
                get_user_mapping_config=lambda: _get_channel_user_mapping_config(
                    channel_id
                ),
            )

            # Create event dispatcher handler
            # Register handler for im.message.receive_v1 event
            handler = self._handler

            def _handle_message_receive(data):
                """Synchronous wrapper for the async event handler.

                The lark-oapi ws client calls event handlers synchronously,
                so we need to bridge to async via asyncio.
                """
                try:
                    event_data = {}
                    # Extract event data from lark SDK event object
                    if hasattr(data, "event"):
                        event = data.event
                        # Build event_data dict from the SDK event object
                        event_data = _extract_event_data(event)
                    else:
                        # Fallback: try to use data directly
                        event_data = data if isinstance(data, dict) else {}

                    if not event_data:
                        logger.warning("[Feishu] Empty event data received")
                        return

                    # Run async handler in the event loop
                    loop = _get_or_create_event_loop()
                    future = asyncio.run_coroutine_threadsafe(
                        handler.handle_feishu_event(event_data), loop
                    )
                    # Wait for result with timeout
                    future.result(timeout=120)

                except Exception as e:
                    logger.exception(
                        "[Feishu] Error handling message event: %s", e
                    )

            event_handler = lark.EventDispatcherHandler.builder(
                "", ""  # Empty verification token and encrypt key for ws mode
            ).register_p2_im_message_receive_v1(
                _handle_message_receive
            ).build()

            # Create WebSocket client
            self._ws_client = lark.ws.Client(
                self.app_id,
                self.app_secret,
                event_handler=event_handler,
                log_level=lark.LogLevel.WARNING,
            )

            # Start ws client in a background thread
            # The ws client's start() method is blocking
            self._ws_thread = threading.Thread(
                target=self._run_ws_client,
                daemon=True,
                name=f"feishu-ws-{self.channel_id}",
            )
            self._ws_thread.start()
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

    def _run_ws_client(self) -> None:
        """Run the WebSocket client in a blocking thread.

        This method is called in a daemon thread and runs the ws client's
        blocking start() method. The SDK handles reconnection internally.
        """
        retry_count = 0
        max_retries = 10
        base_delay = 1.0

        while self._is_running:
            try:
                logger.info(
                    "[Feishu] Channel %s (id=%d) starting WebSocket connection...",
                    self.channel_name,
                    self.channel_id,
                )
                self._ws_client.start()

            except Exception as e:
                if not self._is_running:
                    break

                retry_count += 1
                if retry_count > max_retries:
                    self._set_error(f"Max retries ({max_retries}) exceeded")
                    self._set_running(False)
                    break

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
                import time

                time.sleep(delay)
            else:
                retry_count = 0

        logger.info(
            "[Feishu] Channel %s (id=%d) WebSocket loop exited",
            self.channel_name,
            self.channel_id,
        )

    async def stop(self) -> None:
        """Stop the Feishu WebSocket client."""
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

        # The ws thread is a daemon thread, it will exit when _is_running is False
        # and the ws client disconnects or errors out
        if self._ws_thread and self._ws_thread.is_alive():
            # Give the thread some time to notice _is_running is False
            self._ws_thread.join(timeout=5.0)
            if self._ws_thread.is_alive():
                logger.warning(
                    "[Feishu] Channel %s (id=%d) WebSocket thread did not exit in time",
                    self.channel_name,
                    self.channel_id,
                )

        self._ws_thread = None
        self._ws_client = None
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
            "app_id": f"{self.app_id[:8]}..." if self.app_id else None,
            "default_team_id": self.default_team_id,
        }
        return status


def _extract_event_data(event: Any) -> dict:
    """Extract event data from lark SDK event object into a plain dict.

    The SDK event object has attributes like sender, message, etc.
    We convert to a dict structure for our handler.

    Args:
        event: lark SDK event object

    Returns:
        Dict with sender and message information
    """
    result = {}

    try:
        # Extract sender
        sender = getattr(event, "sender", None)
        if sender:
            sender_id = getattr(sender, "sender_id", None)
            result["sender"] = {
                "sender_id": {
                    "open_id": getattr(sender_id, "open_id", "") if sender_id else "",
                    "user_id": getattr(sender_id, "user_id", "") if sender_id else "",
                    "union_id": getattr(sender_id, "union_id", "") if sender_id else "",
                },
                "sender_type": getattr(sender, "sender_type", ""),
                "tenant_key": getattr(sender, "tenant_key", ""),
            }

        # Extract message
        message = getattr(event, "message", None)
        if message:
            # Extract mentions
            mentions = []
            raw_mentions = getattr(message, "mentions", None)
            if raw_mentions:
                for m in raw_mentions:
                    mention_id = getattr(m, "id", None)
                    mentions.append({
                        "key": getattr(m, "key", ""),
                        "id": {
                            "open_id": getattr(mention_id, "open_id", "") if mention_id else "",
                        },
                        "name": getattr(m, "name", ""),
                    })

            result["message"] = {
                "message_id": getattr(message, "message_id", ""),
                "root_id": getattr(message, "root_id", ""),
                "parent_id": getattr(message, "parent_id", ""),
                "create_time": getattr(message, "create_time", ""),
                "chat_id": getattr(message, "chat_id", ""),
                "chat_type": getattr(message, "chat_type", ""),
                "message_type": getattr(message, "message_type", ""),
                "content": getattr(message, "content", ""),
                "mentions": mentions,
            }

    except Exception as e:
        logger.exception("[Feishu] Error extracting event data: %s", e)

    return result


def _get_or_create_event_loop() -> asyncio.AbstractEventLoop:
    """Get the running event loop or create one for the current thread.

    The lark SDK ws client runs in a separate thread, so we need to
    get the main event loop to schedule async work.

    Returns:
        The asyncio event loop
    """
    try:
        loop = asyncio.get_running_loop()
        return loop
    except RuntimeError:
        pass

    # Try to get the main thread's event loop
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return loop
    except RuntimeError:
        pass

    # Fallback: get or set a new event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop
