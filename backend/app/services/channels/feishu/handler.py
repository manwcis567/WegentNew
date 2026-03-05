# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""
Feishu Channel Message Handler.

This module provides the handler for processing incoming Feishu messages
and integrating them with the Wegent chat system.

Architecture:
- FeishuChannelHandler: Implements BaseChannelHandler for Feishu-specific logic
"""

import asyncio
import json
import logging
from typing import Any, Callable, Dict, Optional

from sqlalchemy.orm import Session

from app.core.cache import cache_manager
from app.db.session import SessionLocal
from app.models.user import User
from app.services.channels.callback import BaseChannelCallbackService, ChannelType
from app.services.channels.feishu.callback import (
    FeishuCallbackInfo,
    feishu_callback_service,
)
from app.services.channels.feishu.sender import FeishuRobotSender
from app.services.channels.feishu.user_resolver import FeishuUserResolver
from app.services.channels.handler import BaseChannelHandler, MessageContext
from app.services.execution.emitters import ResultEmitter
from app.services.subscription.notification_service import (
    subscription_notification_service,
)

logger = logging.getLogger(__name__)

# Message deduplication settings
FEISHU_MSG_DEDUP_PREFIX = "feishu:msg_dedup:"
FEISHU_MSG_DEDUP_TTL = 300  # 5 minutes


class FeishuChannelHandler(BaseChannelHandler):
    """Feishu-specific implementation of BaseChannelHandler.

    This class implements all the abstract methods from BaseChannelHandler
    with Feishu-specific logic for message parsing, user resolution,
    and response sending.
    """

    def __init__(
        self,
        channel_id: int,
        app_id: str,
        app_secret: str,
        get_default_team_id: Optional[Callable[[], Optional[int]]] = None,
        get_default_model_name: Optional[Callable[[], Optional[str]]] = None,
        get_user_mapping_config: Optional[Callable[[], Dict[str, Any]]] = None,
    ):
        """Initialize the Feishu channel handler.

        Args:
            channel_id: The IM channel ID for callback purposes
            app_id: Feishu app ID for API calls
            app_secret: Feishu app secret for API calls
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
        self._app_id = app_id
        self._app_secret = app_secret
        self._sender = FeishuRobotSender(app_id, app_secret)

    def parse_message(self, raw_data: Any) -> MessageContext:
        """Parse Feishu im.message.receive_v1 event data into generic MessageContext.

        The event data structure for im.message.receive_v1:
        {
            "sender": {
                "sender_id": {"open_id": "ou_xxx", "user_id": "xxx", "union_id": "xxx"},
                "sender_type": "user",
                "tenant_key": "xxx"
            },
            "message": {
                "message_id": "om_xxx",
                "root_id": "",
                "parent_id": "",
                "create_time": "1234567890",
                "chat_id": "oc_xxx",
                "chat_type": "p2p" or "group",
                "message_type": "text",
                "content": '{"text":"hello"}',
                "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_xxx"}, "name": "bot"}]
            }
        }

        Args:
            raw_data: Event data dict from Feishu SDK

        Returns:
            MessageContext with parsed message information
        """
        event_data = raw_data
        if not isinstance(event_data, dict):
            return MessageContext(
                content="",
                sender_id="",
                sender_name=None,
                conversation_id="",
                conversation_type="private",
                is_mention=False,
                raw_message=raw_data,
                extra_data={},
            )

        sender = event_data.get("sender", {})
        message = event_data.get("message", {})

        # Extract sender info
        sender_id_info = sender.get("sender_id", {})
        open_id = sender_id_info.get("open_id", "")

        # Extract message info
        message_id = message.get("message_id", "")
        chat_id = message.get("chat_id", "")
        chat_type = message.get("chat_type", "p2p")
        message_type = message.get("message_type", "text")

        # Parse content based on message_type
        content = ""
        content_str = message.get("content", "")
        if content_str:
            try:
                content_json = json.loads(content_str)
                if message_type == "text":
                    content = content_json.get("text", "")
                elif message_type == "post":
                    # Rich text - extract text from post structure
                    content = self._extract_post_text(content_json)
                elif message_type == "image":
                    # Image message - can be handled later
                    content = "[图片]"
                else:
                    content = content_str
            except json.JSONDecodeError:
                content = content_str

        # Clean up @bot mentions from content
        mentions = message.get("mentions", [])
        is_mention = False
        if mentions:
            for mention in mentions:
                mention_key = mention.get("key", "")
                if mention_key and mention_key in content:
                    content = content.replace(mention_key, "").strip()
                    is_mention = True

        # Build extra_data
        extra_data = {
            "message_id": message_id,
            "chat_id": chat_id,
            "open_id": open_id,
            "message_type": message_type,
            "user_id": sender_id_info.get("user_id"),
            "union_id": sender_id_info.get("union_id"),
            "tenant_key": sender.get("tenant_key"),
        }

        return MessageContext(
            content=content.strip(),
            sender_id=open_id,
            sender_name=None,  # Feishu events don't include sender name directly
            conversation_id=chat_id,
            conversation_type="group" if chat_type == "group" else "private",
            is_mention=is_mention,
            raw_message=event_data,
            extra_data=extra_data,
        )

    def _extract_post_text(self, post_content: dict) -> str:
        """Extract plain text from Feishu post (rich text) content.

        Args:
            post_content: Post content dict

        Returns:
            Extracted plain text
        """
        texts = []
        # Post content has locale keys like "zh_cn" with "content" list
        for locale_data in post_content.values():
            if isinstance(locale_data, dict):
                content_list = locale_data.get("content", [])
                for paragraph in content_list:
                    if isinstance(paragraph, list):
                        for element in paragraph:
                            if isinstance(element, dict) and element.get("tag") == "text":
                                texts.append(element.get("text", ""))
        return " ".join(texts) if texts else ""

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
            sender_name=message_context.sender_name,
        )

    async def send_text_reply(self, message_context: MessageContext, text: str) -> bool:
        """Send a text reply to Feishu.

        Uses the reply API if we have message_id, otherwise sends to chat.

        Args:
            message_context: Original message context
            text: Text to send

        Returns:
            True if sent successfully, False otherwise
        """
        try:
            message_id = message_context.extra_data.get("message_id")
            chat_id = message_context.extra_data.get("chat_id")

            if message_id:
                result = await self._sender.reply_text_message(message_id, text)
            elif chat_id:
                result = await self._sender.send_text_to_chat(chat_id, text)
            else:
                self.logger.error("[FeishuHandler] No message_id or chat_id for reply")
                return False

            return result.get("success", False)

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
            chat_id=message_context.extra_data.get("chat_id"),
            message_id=message_context.extra_data.get("message_id"),
            open_id=message_context.extra_data.get("open_id"),
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

        Currently returns None as Feishu doesn't support streaming cards
        like DingTalk's AI Card. The system will fall back to SyncResponseEmitter.

        Args:
            message_context: Message context

        Returns:
            None (not supported yet)
        """
        return None

    async def handle_feishu_event(self, event_data: dict) -> bool:
        """Handle an incoming Feishu event.

        This is the main entry point called from the FeishuChannelProvider.
        It handles deduplication and delegates to the base handler.

        Args:
            event_data: The event data from Feishu SDK

        Returns:
            True if handled successfully, False otherwise
        """
        message = event_data.get("message", {})
        message_id = message.get("message_id", "")

        # Deduplicate messages
        if message_id:
            dedup_key = f"{FEISHU_MSG_DEDUP_PREFIX}{message_id}"
            is_new = await cache_manager.setnx(
                dedup_key, "1", expire=FEISHU_MSG_DEDUP_TTL
            )
            if not is_new:
                self.logger.warning(
                    "[FeishuHandler] Duplicate message detected, skipping: message_id=%s",
                    message_id,
                )
                return True  # Return True to prevent retries

        self.logger.info(
            "[FeishuHandler] Received message: message_id=%s, chat_type=%s",
            message_id,
            message.get("chat_type", "unknown"),
        )

        # Parse and process through base handler
        message_context = self.parse_message(event_data)

        # Update IM binding for subscription notifications
        db = SessionLocal()
        try:
            user = await self.resolve_user(db, message_context)
            if user and self._channel_id:
                try:
                    subscription_notification_service.update_user_im_binding(
                        db=db,
                        user_id=user.id,
                        channel_id=self._channel_id,
                        channel_type="feishu",
                        sender_id=message_context.sender_id,
                        sender_staff_id=message_context.extra_data.get("user_id"),
                        conversation_id=message_context.conversation_id,
                    )
                except Exception as e:
                    self.logger.warning(
                        "[FeishuHandler] Failed to update IM binding: %s", e
                    )
        finally:
            db.close()

        return await self.handle_message(event_data)
