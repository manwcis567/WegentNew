# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""
Feishu (Lark) Channel Handler.

This module provides the handler for processing incoming Feishu messages
and integrating them with the Wegent chat system.

Supports multiple execution modes:
- Chat Shell: Direct LLM conversation (default)
- Local Device: Execute tasks on user's local device
- Cloud Executor: Execute tasks on cloud Docker container
"""

import json
import logging
from typing import Any, Callable, Dict, Optional

from lark_oapi import Client
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import (
    P2ImMessageReceiveV1 as ImReceiveEvent,
)
from sqlalchemy.orm import Session

from app.core.cache import cache_manager
from app.db.session import SessionLocal
from app.models.user import User
from app.services.channels.callback import BaseChannelCallbackService, ChannelType
from app.services.channels.emitter import SyncResponseEmitter
from app.services.channels.feishu.callback import (
    FeishuCallbackInfo,
    feishu_callback_service,
)
from app.services.channels.feishu.emitter import FeishuStreamingResponseEmitter
from app.services.channels.feishu.user_resolver import FeishuUserResolver
from app.services.channels.handler import BaseChannelHandler, MessageContext
from app.services.execution.emitters import ResultEmitter

logger = logging.getLogger(__name__)

# Message deduplication settings
# Feishu may retry sending messages if ACK is not received in time
FEISHU_MSG_DEDUP_PREFIX = "feishu:msg_dedup:"
FEISHU_MSG_DEDUP_TTL = 300  # 5 minutes


class FeishuChannelHandler(BaseChannelHandler[ImReceiveEvent, FeishuCallbackInfo]):
    """Feishu-specific implementation of BaseChannelHandler.

    This class implements all the abstract methods from BaseChannelHandler
    with Feishu-specific logic for message parsing, user resolution,
    and response sending.
    """

    def __init__(
        self,
        channel_id: int,
        client: Optional[Client] = None,
        get_default_team_id: Optional[Callable[[], Optional[int]]] = None,
        get_default_model_name: Optional[Callable[[], Optional[str]]] = None,
        get_user_mapping_config: Optional[Callable[[], Dict[str, Any]]] = None,
    ):
        """Initialize the Feishu channel handler.

        Args:
            channel_id: The IM channel ID for callback purposes
            client: Feishu client for sending responses
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
        self._client = client

    def set_client(self, client: Client) -> None:
        """Set the Feishu client (can be set after initialization)."""
        self._client = client

    def parse_message(self, raw_data: Any) -> MessageContext:
        """Parse Feishu ImReceiveEvent into generic MessageContext.

        Args:
            raw_data: ImReceiveEvent from Feishu SDK

        Returns:
            MessageContext with parsed message information
        """
        event: ImReceiveEvent = raw_data

        # Extract message data
        message_data = event.event.message if event.event else None
        sender_data = event.event.sender if event.event else None

        # Extract text content
        content = ""
        if message_data:
            content_str = getattr(message_data, "content", "")
            if content_str:
                try:
                    content_json = json.loads(content_str)
                    content = content_json.get("text", "").strip()
                except (json.JSONDecodeError, AttributeError):
                    content = str(content_str).strip()

        # Extract sender info
        sender_id = ""
        sender_name = ""
        sender_type = ""
        if sender_data:
            sender_id = getattr(sender_data, "sender_id", {}).get("open_id", "")
            sender_name = getattr(sender_data, "sender_name", "")
            sender_type = getattr(sender_data, "sender_type", "user")

        # Extract conversation info
        conversation_id = ""
        conversation_type = "private"
        if message_data:
            conversation_id = getattr(message_data, "chat_id", "")
            chat_type = getattr(message_data, "chat_type", "")
            if chat_type == "group":
                conversation_type = "group"

        # Check if bot was mentioned (for group chats)
        is_mention = False
        if conversation_type == "group":
            # Check if message mentions the bot
            bot_id = getattr(sender_data, "bot_id", "") if sender_data else ""
            if bot_id and content and f"@{bot_id}" in content:
                is_mention = True

        # Build extra_data
        extra_data = {
            "sender_id_type": "open_id",
            "sender_type": sender_type,
            "message_id": (
                getattr(message_data, "message_id", "") if message_data else ""
            ),
            "root_id": getattr(message_data, "root_id", "") if message_data else "",
            "parent_id": getattr(message_data, "parent_id", "") if message_data else "",
            "create_time": (
                getattr(message_data, "create_time", "") if message_data else ""
            ),
            "chat_type": getattr(message_data, "chat_type", "") if message_data else "",
        }

        # Include event data for callback
        if event.event:
            extra_data["event_data"] = {
                "message": (
                    event.event.message.to_dict()
                    if hasattr(event.event.message, "to_dict")
                    else {}
                ),
                "sender": (
                    event.event.sender.to_dict()
                    if hasattr(event.event.sender, "to_dict")
                    else {}
                ),
            }

        return MessageContext(
            content=content,
            sender_id=sender_id,
            sender_name=sender_name if sender_name else f"User_{sender_id[:8]}",
            conversation_id=conversation_id,
            conversation_type=conversation_type,
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

        # Get sender's open_id and union_id
        sender_id = message_context.sender_id
        event_data = message_context.extra_data.get("event_data", {})
        sender_info = event_data.get("sender", {})
        union_id = sender_info.get("union_id", "")

        return await resolver.resolve_user(
            sender_id=sender_id,
            sender_name=message_context.sender_name,
            union_id=union_id,
        )

    async def send_text_reply(self, message_context: MessageContext, text: str) -> bool:
        """Send a text reply to Feishu.

        Args:
            message_context: Original message context
            text: Text to send

        Returns:
            True if sent successfully, False otherwise
        """
        if not self._client:
            self.logger.error("[FeishuHandler] Client not initialized")
            return False

        try:
            from lark.oapi.api.im.v1 import (
                CreateMessageReqBody,
                CreateMessageRequest,
            )

            # Get receive_id and determine receive_id_type
            event_data = message_context.extra_data.get("event_data", {})
            sender_info = event_data.get("sender", {})
            receive_id = sender_info.get("user_id", message_context.sender_id)
            receive_id_type = "open_id"  # Default to open_id

            # Prepare message content
            content_json = json.dumps({"text": text})

            req = (
                CreateMessageRequest.builder()
                .request_body(
                    CreateMessageReqBody.builder()
                    .receive_id(receive_id)
                    .msg_type("text")
                    .content(content_json)
                    .build()
                )
                .build()
            )

            resp = await self._client.im.message.create(req)

            if resp.code == 0:
                self.logger.info("[FeishuHandler] Message sent successfully")
                return True
            else:
                self.logger.error(
                    "[FeishuHandler] Failed to send message: code=%s, msg=%s",
                    resp.code,
                    resp.msg,
                )
                return False

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
        event_data = message_context.extra_data.get("event_data", {})

        return FeishuCallbackInfo(
            channel_id=self._channel_id,
            conversation_id=message_context.conversation_id,
            sender_id=message_context.sender_id,
            receive_id=message_context.sender_id,
            event_data=event_data,
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
        """Create a streaming emitter for Feishu message updates.

        Args:
            message_context: Message context

        Returns:
            FeishuStreamingResponseEmitter or None if not supported
        """
        if not self._client:
            return None

        return FeishuStreamingResponseEmitter(
            client=self._client,
            message_context=message_context,
        )

    async def handle_event(self, event: ImReceiveEvent) -> None:
        """Handle incoming Feishu event.

        This method is called by the Feishu event dispatcher when a message
        is received.

        Args:
            event: Feishu message receive event
        """
        try:
            # Parse the event into MessageContext
            message_context = self.parse_message(event)

            # Deduplicate messages using message_id
            message_id = message_context.extra_data.get("message_id")
            if message_id:
                dedup_key = f"{FEISHU_MSG_DEDUP_PREFIX}{message_id}"
                is_new = await cache_manager.setnx(
                    dedup_key, "1", expire=FEISHU_MSG_DEDUP_TTL
                )
                if not is_new:
                    self.logger.warning(
                        "[FeishuHandler] Duplicate message detected, skipping: msgId=%s",
                        message_id,
                    )
                    return

            # Delegate to the base handler's message processing
            await self.handle_message(event)

        except Exception as e:
            self.logger.exception(f"[FeishuHandler] Error processing event: {e}")
