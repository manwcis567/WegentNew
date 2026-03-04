# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""
Feishu (Lark) IM Channel Handler.

This module provides the handler for processing incoming Feishu messages
and integrating them with the Wegent chat system.

Supports multiple execution modes:
- Chat Shell: Direct LLM conversation (default)
- Local Device: Execute tasks on user's local device
- Cloud Executor: Execute tasks on cloud Docker container

Architecture:
- FeishuChannelHandler: Implements BaseChannelHandler for Feishu-specific logic
- WegentFeishuHandler: Main handler that delegates to FeishuChannelHandler
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from sqlalchemy.orm import Session

from app.core.cache import cache_manager
from app.db.session import SessionLocal
from app.models.user import User
from app.services.channels.callback import BaseChannelCallbackService, ChannelType
from app.services.channels.feishu.callback import FeishuCallbackInfo, feishu_callback_service
from app.services.channels.feishu.emitter import StreamingResponseEmitter
from app.services.channels.feishu.sender import FeishuSender
from app.services.channels.feishu.user_resolver import FeishuUserResolver
from app.services.channels.handler import BaseChannelHandler, MessageContext
from app.services.execution.emitters import ResultEmitter

if TYPE_CHECKING:
    from app.services.channels.feishu.service import FeishuChannelProvider

logger = logging.getLogger(__name__)

# Message deduplication settings
# Feishu may retry sending messages if ACK is not received in time
FEISHU_MSG_DEDUP_PREFIX = "feishu:msg_dedup:"
FEISHU_MSG_DEDUP_TTL = 300  # 5 minutes - enough to cover retry window


class FeishuChannelHandler(BaseChannelHandler[Dict[str, Any], FeishuCallbackInfo]):
    """Feishu-specific implementation of BaseChannelHandler.

    This class implements all the abstract methods from BaseChannelHandler
    with Feishu-specific logic for message parsing, user resolution,
    and response sending.
    """

    def __init__(
        self,
        channel_id: int,
        get_default_team_id: Optional[Callable[[], Optional[int]]] = None,
        get_default_model_name: Optional[Callable[[], Optional[str]]] = None,
        get_user_mapping_config: Optional[Callable[[], Dict[str, Any]]] = None,
    ):
        """Initialize the Feishu channel handler.

        Args:
            channel_id: The IM channel ID for callback purposes
            get_default_team_id: Callback to get current default_team_id dynamically
            get_default_model_name: Callback to get current default_model_name dynamically
            get_user_mapping_config: Callback to get user mapping configuration dynamically
        """
        super().__init__(
            channel_type=ChannelType.FEISHU,
            channel_id=channel_id,
            get_default_team_id=get_default_team_id,
            get_default_model_name=get_default_model_name,
            get_user_mapping_config=get_user_mapping_config,
        )
        self._provider: Optional["FeishuChannelProvider"] = None
        self._sender: Optional[FeishuSender] = None

    def set_provider(self, provider: "FeishuChannelProvider") -> None:
        """Set the channel provider reference."""
        self._provider = provider
        if provider and provider._app_id:
            self._sender = FeishuSender(
                app_id=provider._app_id,
                app_secret=provider.app_secret,
                aes_key=provider._aes_key,
                token=provider._token,
            )

    def parse_message(self, raw_data: Any) -> MessageContext:
        """Parse Feishu message event into generic MessageContext.

        Args:
            raw_data: Message event dict from Feishu

        Returns:
            MessageContext with parsed message information
        """
        event = raw_data
        self._current_event = event

        # Extract message content
        message = event.get("event", {}).get("message", {})

        # Extract text content
        content = ""
        message_type = message.get("message_type", "")

        if message_type == "text":
            content = message.get("content", "").strip()

        # Extract conversation info
        chat_id = message.get("chat_id", "")
        chat_type = message.get("chat_type", "private")

        # Extract sender info
        sender_id = event.get("event", {}).get("sender", {}).get("sender_id", {}).get("open_id", "")
        sender_nick = event.get("event", {}).get("sender", {}).get("sender_nick", "")

        # Check if bot was mentioned
        at_list = message.get("at_list", [])
        is_mention = any(
            at.get("encekey", "") == "self" for at in at_list
        ) if at_list else False

        # Build extra_data with callback data
        extra_data = {
            "message_id": message.get("message_id", ""),
            "chat_type": chat_type,
            "at_list": at_list,
        }

        return MessageContext(
            content=content,
            sender_id=sender_id,
            sender_name=sender_nick,
            conversation_id=chat_id,
            conversation_type="group" if chat_type == "group" else "private",
            is_mention=is_mention,
            raw_message=event,
            extra_data=extra_data,
        )

    async def resolve_user(
        self, db: Session, message_context: MessageContext
    ) -> Optional[User]:
        """Resolve Feishu user to Wegent user.

        Args:
            db: Database session
            message_context: Parsed message context

        Returns:
            Wegent User or None if not found
        """
        mapping_config = self.user_mapping_config
        resolver = FeishuUserResolver(
            db,
            user_mapping_mode=mapping_config.mode,
            user_mapping_config=mapping_config.config,
        )
        return await resolver.resolve_user(
            open_id=message_context.sender_id,
            nick_name=message_context.sender_name,
        )

    async def send_text_reply(self, message_context: MessageContext, text: str) -> bool:
        """Send a text reply to Feishu.

        Args:
            message_context: Original message context
            text: Text to send

        Returns:
            True if sent successfully, False otherwise
        """
        if not self._sender:
            self.logger.error("[FeishuHandler] Sender not initialized")
            return False

        raw_message = message_context.raw_message
        if not isinstance(raw_message, dict):
            self.logger.error("[FeishuHandler] Invalid raw_message type for reply")
            return False

        # Extract chat_id from the message
        chat_id = raw_message.get("event", {}).get("message", {}).get("chat_id", "")

        if not chat_id:
            self.logger.error("[FeishuHandler] Missing chat_id for reply")
            return False

        try:
            # Send the text message
            result = await self._sender.send_text(chat_id, text)
            return result.get("code", 0) == 0
        except Exception as e:
            self.logger.exception(f"[FeishuHandler] Failed to send reply: {e}")
            return False

    def create_callback_info(
        self, message_context: MessageContext
    ) -> FeishuCallbackInfo:
        """Create Feishu callback info for task completion notification.

        Args:
            message_context: Message context

        Returns:
            FeishuCallbackInfo instance
        """
        return FeishuCallbackInfo(
            channel_id=self._channel_id,
            conversation_id=message_context.conversation_id,
            incoming_message_data=message_context.extra_data.get("callback_data"),
        )

    def get_callback_service(self) -> Optional[BaseChannelCallbackService]:
        """Get the Feishu callback service.

        Returns:
            FeishuCallbackService instance
        """
        return feishu_callback_service

    async def create_streaming_emitter(
        self, message_context: MessageContext
    ) -> Optional[ResultEmitter]:
        """Create a streaming emitter for Feishu.

        Feishu uses a different approach for streaming - we use the
        sender to send multiple messages as updates.

        Args:
            message_context: Message context

        Returns:
            StreamingResponseEmitter or None if not supported
        """
        if not self._sender:
            return None

        return StreamingResponseEmitter(
            sender=self._sender,
            conversation_id=message_context.conversation_id,
        )

    def set_sender(self, sender: FeishuSender) -> None:
        """Set the Feishu sender (for testing or late initialization)."""
        self._sender = sender


class WegentFeishuHandler:
    """Handler for Feishu message events.

    This handler receives messages from Feishu via the webhook endpoint
    and delegates processing to FeishuChannelHandler which inherits from
    the generic BaseChannelHandler.

    This design allows:
    1. Compliance with Feishu SDK's handler interface
    2. Reuse of common channel handling logic from BaseChannelHandler
    """

    def __init__(
        self,
        default_team_id: Optional[int] = None,
        get_default_team_id: Optional[Callable[[], Optional[int]]] = None,
        get_default_model_name: Optional[Callable[[], Optional[str]]] = None,
        get_user_mapping_config: Optional[Callable[[], Dict[str, Any]]] = None,
        channel_id: Optional[int] = None,
    ):
        """Initialize the handler.

        Args:
            default_team_id: Default team ID for this channel (deprecated)
            get_default_team_id: Callback to get current default_team_id dynamically.
            get_default_model_name: Callback to get current default_model_name dynamically.
            get_user_mapping_config: Callback to get user mapping configuration.
            channel_id: The IM channel ID (Kind.id) for IM binding tracking and callback purposes.
        """
        self._channel_id = channel_id or 0

        # Handle deprecated default_team_id parameter
        if get_default_team_id is None and default_team_id is not None:
            get_default_team_id = lambda tid=default_team_id: tid

        # Create the internal channel handler that does the actual work
        self._channel_handler = FeishuChannelHandler(
            channel_id=self._channel_id,
            get_default_team_id=get_default_team_id,
            get_default_model_name=get_default_model_name,
            get_user_mapping_config=get_user_mapping_config,
        )

        self.logger = logging.getLogger(__name__)

    @property
    def default_team_id(self) -> Optional[int]:
        """Get the current default team ID."""
        return self._channel_handler.default_team_id

    @property
    def default_model_name(self) -> Optional[str]:
        """Get the current default model name."""
        return self._channel_handler.default_model_name

    async def process(self, event_data: Dict[str, Any]) -> dict:
        """Process incoming Feishu message event.

        This method is called when a message event is received from Feishu.

        Args:
            event_data: The full event data from Feishu webhook

        Returns:
            Response dict to acknowledge receipt
        """
        try:
            # Extract message info for logging
            event = event_data.get("event", {})
            message = event.get("message", {})

            self.logger.info(
                "[FeishuHandler] Received message: sender=%s, msg_id=%s, content_preview=%s",
                event.get("sender", {}).get("sender_nick", "unknown"),
                message.get("message_id", "unknown")[:8],
                message.get("content", "")[:50] if message.get("content") else "empty",
            )

            # Deduplicate messages using message_id
            message_id = message.get("message_id", "")
            if message_id:
                dedup_key = f"{FEISHU_MSG_DEDUP_PREFIX}{message_id}"
                # Try to set the key with SETNX (only if not exists)
                is_new = await cache_manager.setnx(
                    dedup_key, "1", expire=FEISHU_MSG_DEDUP_TTL
                )
                if not is_new:
                    self.logger.warning(
                        "[FeishuHandler] Duplicate message detected, skipping: msg_id=%s",
                        message_id,
                    )
                    return {"status": 200, "message": "OK (duplicate)"}

            # Store the event for the handler
            incoming_event = event

            # Build message context
            message_context = self._channel_handler.parse_message(event_data)
            message_context.extra_data["callback_data"] = event_data

            # Get user and update IM binding for subscription notifications
            db = SessionLocal()
            try:
                user = await self._channel_handler.resolve_user(db, message_context)
                if user and self._channel_id:
                    try:
                        from app.services.subscription.notification_service import (
                            subscription_notification_service,
                        )
                        subscription_notification_service.update_user_im_binding(
                            db=db,
                            user_id=user.id,
                            channel_id=self._channel_id,
                            channel_type="feishu",
                            sender_id=message_context.sender_id,
                            sender_open_id=message_context.sender_id,
                            conversation_id=message_context.conversation_id,
                        )
                    except Exception as e:
                        self.logger.warning(
                            "[FeishuHandler] Failed to update IM binding: %s", e
                        )
            finally:
                db.close()

            # Process through the channel handler
            success = await self._channel_handler.handle_message(event_data)

            if success:
                return {"status": 200, "message": "ok"}
            else:
                return {"status": 500, "message": "Failed to process message"}

        except Exception as e:
            self.logger.exception("[FeishuHandler] Error processing message: %s", e)
            return {"status": 500, "message": f"Internal Server Error: {e}"}

    async def handle_message(self, event_data: Dict[str, Any]) -> bool:
        """Handle a message event.

        This is a convenience method for direct message handling.

        Args:
            event_data: The full event data from Feishu

        Returns:
            True if handled successfully
        """
        return await self._channel_handler.handle_message(event_data)

    def set_sender(self, sender: "FeishuSender") -> None:
        """Set the Feishu sender."""
        self._channel_handler.set_sender(sender)
