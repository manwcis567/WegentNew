# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""
Feishu Callback Service.

This module provides the callback service for Feishu channel integration.
It handles sending task completion notifications and streaming updates
back to Feishu users.
"""

import json
import logging
from typing import Any, Dict, Optional

from lark_oapi import Client

from app.services.channels.callback import (
    BaseCallbackInfo,
    BaseChannelCallbackService,
    ChannelType,
)
from app.services.channels.feishu.emitter import FeishuStreamingResponseEmitter
from app.services.execution.emitters import ResultEmitter

logger = logging.getLogger(__name__)


class FeishuCallbackInfo(BaseCallbackInfo):
    """Callback info for Feishu channel."""

    sender_id: str  # Feishu sender ID (open_id)
    receive_id: str  # ID to use for sending replies
    event_data: Dict[str, Any]  # Original event data for reference

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Redis storage."""
        return {
            **super().to_dict(),
            "sender_id": self.sender_id,
            "receive_id": self.receive_id,
            "event_data": self.event_data,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FeishuCallbackInfo":
        """Create from dictionary."""
        return cls(
            channel_type=ChannelType(data.get("channel_type", "feishu")),
            channel_id=data.get("channel_id", 0),
            conversation_id=data.get("conversation_id", ""),
            sender_id=data.get("sender_id", ""),
            receive_id=data.get("receive_id", ""),
            event_data=data.get("event_data", {}),
        )


class FeishuCallbackService(BaseChannelCallbackService[FeishuCallbackInfo]):
    """
    Callback service for Feishu channel.

    Handles sending task completion notifications and streaming updates
    back to Feishu users.
    """

    def __init__(self):
        """Initialize the Feishu callback service."""
        super().__init__(channel_type=ChannelType.FEISHU)
        self._clients: Dict[int, Client] = {}  # channel_id -> client

    def register_client(self, channel_id: int, client: Client) -> None:
        """
        Register a Feishu client for a channel.

        Args:
            channel_id: The IM channel ID
            client: Feishu client instance
        """
        self._clients[channel_id] = client
        logger.debug("[FeishuCallback] Registered client for channel %d", channel_id)

    def unregister_client(self, channel_id: int) -> None:
        """
        Unregister a Feishu client.

        Args:
            channel_id: The IM channel ID
        """
        if channel_id in self._clients:
            del self._clients[channel_id]
            logger.debug(
                "[FeishuCallback] Unregistered client for channel %d", channel_id
            )

    def _get_client(self, channel_id: int) -> Optional[Client]:
        """
        Get the Feishu client for a channel.

        Args:
            channel_id: The IM channel ID

        Returns:
            Feishu client or None if not registered
        """
        return self._clients.get(channel_id)

    async def _create_emitter(
        self, task_id: int, subtask_id: int, callback_info: FeishuCallbackInfo
    ) -> Optional[ResultEmitter]:
        """
        Create a streaming emitter for Feishu.

        Args:
            task_id: Task ID
            subtask_id: Subtask ID
            callback_info: Feishu callback info

        Returns:
            ResultEmitter or None if client not available
        """
        client = self._get_client(callback_info.channel_id)
        if not client:
            logger.warning(
                "[FeishuCallback] No client registered for channel %d",
                callback_info.channel_id,
            )
            return None

        return FeishuStreamingResponseEmitter(
            client=client,
            callback_info=callback_info,
        )

    def _parse_callback_info(self, callback_data: Dict[str, Any]) -> FeishuCallbackInfo:
        """
        Parse callback data into FeishuCallbackInfo.

        Args:
            callback_data: Callback data from Redis

        Returns:
            FeishuCallbackInfo instance
        """
        return FeishuCallbackInfo.from_dict(callback_data)

    def _extract_thinking_display(self, callback_info: FeishuCallbackInfo) -> str:
        """
        Extract display text for thinking mode.

        Args:
            callback_info: Feishu callback info

        Returns:
            Display text for thinking mode
        """
        return f"会话 ID: {callback_info.conversation_id}"


# Singleton instance
feishu_callback_service = FeishuCallbackService()
