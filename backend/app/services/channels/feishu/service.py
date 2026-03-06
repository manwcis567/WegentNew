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
import json
import logging
import threading
from dataclasses import is_dataclass
from typing import Any, Callable, Dict, Optional

from app.services.channels.base import BaseChannelProvider

logger = logging.getLogger(__name__)

# CRD kind for IM channels
MESSAGER_KIND = "Messager"
MESSAGER_USER_ID = 0

FEISHU_MESSAGE_EVENT = "im.message.receive_v1"
FEISHU_LEGACY_CARD_CALLBACK_EVENT = "card.action.trigger"
FEISHU_CALLBACK_REGISTRATIONS: tuple[tuple[str, str], ...] = (
    (FEISHU_MESSAGE_EVENT, "register_p2_im_message_receive_v1"),
    (FEISHU_LEGACY_CARD_CALLBACK_EVENT, "register_p2_card_action_trigger"),
    ("application.bot.menu_v6", "register_p2_application_bot_menu_v6"),
    (
        "im.chat.access_event.bot_p2p_chat_entered_v1",
        "register_p2_im_chat_access_event_bot_p2p_chat_entered_v1",
    ),
)


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
        """Get the Feishu app ID from config."""
        return self.config.get("app_id") or self.config.get("client_id")

    @property
    def app_secret(self) -> Optional[str]:
        """Get the Feishu app secret from config."""
        return self.config.get("app_secret") or self.config.get("client_secret")

    @property
    def verification_token(self) -> Optional[str]:
        """Get the optional verification token for HTTP callback mode."""
        return self.config.get("verification_token") or self.config.get("token")

    @property
    def encrypt_key(self) -> Optional[str]:
        """Get the optional encrypt key for HTTP callback mode."""
        return self.config.get("encrypt_key")

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
                    logger.exception("[Feishu] Error handling message event: %s", e)

            dispatcher_builder = lark.EventDispatcherHandler.builder(
                "", ""  # Empty verification token and encrypt key for ws mode
            )

            for event_name, register_method_name in FEISHU_CALLBACK_REGISTRATIONS:
                register_method = getattr(
                    dispatcher_builder, register_method_name, None
                )
                if register_method is None:
                    logger.info(
                        "[Feishu] SDK does not expose %s for %s, skipping",
                        register_method_name,
                        event_name,
                    )
                    continue

                register_method(
                    self._create_event_callback(
                        event_name=event_name,
                        message_handler=_handle_message_receive,
                    )
                )

            event_handler = dispatcher_builder.build()

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
            "supports_long_connection_callbacks": [
                event_name
                for event_name, register_method_name in FEISHU_CALLBACK_REGISTRATIONS
                if register_method_name != "register_p2_im_message_receive_v1"
            ],
            "legacy_callback_path": f"/api/channels/feishu/callbacks/{self.channel_id}",
        }
        return status

    def _create_event_callback(
        self,
        event_name: str,
        message_handler: Callable[[Any], None],
    ) -> Callable[[Any], None]:
        """Create a sync SDK callback that dispatches to the async handler."""

        def _callback(data: Any) -> None:
            try:
                event_data = _extract_event_payload(data)
                if not event_data:
                    logger.warning("[Feishu] Empty payload received for %s", event_name)
                    return

                if event_name == FEISHU_MESSAGE_EVENT:
                    message_handler(event_data)
                    return

                loop = _get_or_create_event_loop()
                future = asyncio.run_coroutine_threadsafe(
                    self._handler.handle_callback_event(event_name, event_data),
                    loop,
                )
                future.result(timeout=120)
            except Exception as e:
                logger.exception(
                    "[Feishu] Error handling %s callback: %s", event_name, e
                )

        return _callback


def _extract_event_payload(data: Any) -> dict:
    """Extract a plain dict payload from lark SDK callback/event objects."""
    try:
        if isinstance(data, dict):
            if isinstance(data.get("event"), dict):
                return data["event"]
            return data

        event = getattr(data, "event", None)
        if event is not None:
            return _to_plain_data(event)

        return _to_plain_data(data)
    except Exception as e:
        logger.exception("[Feishu] Error extracting event payload: %s", e)
        return {}


def _to_plain_data(value: Any) -> Any:
    """Convert SDK objects into plain Python data structures."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {key: _to_plain_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_plain_data(item) for item in value]
    if is_dataclass(value):
        return {
            key: _to_plain_data(item)
            for key, item in value.__dict__.items()
            if not key.startswith("_")
        }

    if hasattr(value, "to_dict"):
        maybe_dict = value.to_dict()
        if isinstance(maybe_dict, dict):
            return _to_plain_data(maybe_dict)

    if hasattr(value, "__dict__"):
        data = {}
        for key, item in vars(value).items():
            if key.startswith("_") or callable(item):
                continue
            data[key] = _to_plain_data(item)
        if data:
            return data

    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")

    if hasattr(value, "__iter__") and not isinstance(value, (str, bytes)):
        return [_to_plain_data(item) for item in value]

    try:
        return json.loads(str(value))
    except Exception:
        return str(value)


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
