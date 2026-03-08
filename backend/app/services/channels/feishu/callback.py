# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""
Feishu callback service for device/cloud task execution.

This module provides functionality to send streaming updates and task completion
results back to Feishu when tasks are executed on devices or cloud executors.

Supports:
- Task completion notifications via text reply
- Streaming progress updates (using SyncResponseEmitter initially)
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional

from app.services.channels.callback import (
    BaseCallbackInfo,
    BaseChannelCallbackService,
    ChannelType,
    get_callback_registry,
)

if TYPE_CHECKING:
    from app.services.execution.emitters import ResultEmitter

logger = logging.getLogger(__name__)


@dataclass
class FeishuCallbackInfo(BaseCallbackInfo):
    """Information needed to send callback to Feishu."""

    chat_id: Optional[str] = None  # Feishu chat ID
    message_id: Optional[str] = None  # Message ID for reply
    open_id: Optional[str] = None  # Sender open_id

    def __init__(
        self,
        channel_id: int,
        conversation_id: str,
        chat_id: Optional[str] = None,
        message_id: Optional[str] = None,
        open_id: Optional[str] = None,
    ):
        """Initialize FeishuCallbackInfo.

        Args:
            channel_id: Feishu channel ID for getting client
            conversation_id: Feishu conversation ID (chat_id)
            chat_id: Feishu chat ID
            message_id: Message ID for reply
            open_id: Sender open_id
        """
        super().__init__(
            channel_type=ChannelType.FEISHU,
            channel_id=channel_id,
            conversation_id=conversation_id,
        )
        self.chat_id = chat_id
        self.message_id = message_id
        self.open_id = open_id

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Redis storage."""
        data = super().to_dict()
        data.update(
            {
                "chat_id": self.chat_id,
                "message_id": self.message_id,
                "open_id": self.open_id,
            }
        )
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FeishuCallbackInfo":
        """Create from dictionary."""
        return cls(
            channel_id=data.get("channel_id", 0),
            conversation_id=data.get("conversation_id", ""),
            chat_id=data.get("chat_id"),
            message_id=data.get("message_id"),
            open_id=data.get("open_id"),
        )


class FeishuCallbackService(BaseChannelCallbackService[FeishuCallbackInfo]):
    """Service for managing Feishu task callbacks and streaming updates."""

    def __init__(self):
        """Initialize the callback service."""
        super().__init__(ChannelType.FEISHU)

    def _parse_callback_info(self, data: Dict[str, Any]) -> FeishuCallbackInfo:
        """Parse callback info from dictionary."""
        return FeishuCallbackInfo.from_dict(data)

    def _extract_thinking_display(self, thinking: Any) -> str:
        """Extract the latest thinking step in human-readable format.

        Same logic as DingTalk - extracts the latest thinking step
        for display in the channel.
        """
        if not thinking:
            return ""

        if not isinstance(thinking, list) or len(thinking) == 0:
            return ""

        latest = thinking[-1]
        if not isinstance(latest, dict):
            return str(latest) if latest else ""

        details = latest.get("details", {})
        if not isinstance(details, dict):
            return ""

        detail_type = details.get("type", "")

        if detail_type == "tool_use":
            tool_name = details.get("name", "") or details.get("tool_name", "")
            if tool_name:
                return f"工具使用: {tool_name}"
            return "工具使用中..."

        elif detail_type == "tool_result":
            tool_name = details.get("tool_name", "") or details.get("name", "")
            is_error = details.get("is_error", False)
            if is_error:
                return f"工具执行失败: {tool_name}"
            if tool_name:
                return f"工具完成: {tool_name}"
            return "工具执行完成"

        elif detail_type == "assistant":
            message = details.get("message", {})
            if isinstance(message, dict):
                content_list = message.get("content", [])
                if isinstance(content_list, list):
                    for content_item in content_list:
                        if isinstance(content_item, dict):
                            content_type = content_item.get("type", "")
                            if content_type == "tool_use":
                                tool_name = content_item.get("name", "")
                                if tool_name:
                                    return f"工具使用: {tool_name}"
                                return "工具使用中..."
                            elif content_type == "text":
                                text = content_item.get("text", "")
                                if text:
                                    if len(text) > 100:
                                        return f"{text[:100]}..."
                                    return text
            return "思考中..."

        elif detail_type == "text":
            message = details.get("message", {})
            if isinstance(message, dict):
                content_list = message.get("content", [])
                if isinstance(content_list, list):
                    for content_item in content_list:
                        if isinstance(content_item, dict):
                            text = content_item.get("text", "")
                            if text:
                                if len(text) > 100:
                                    return f"{text[:100]}..."
                                return text
            return "思考中..."

        elif detail_type == "system":
            subtype = details.get("subtype", "")
            if subtype == "init":
                return "系统初始化"
            return "系统消息"

        elif detail_type == "result":
            return "生成结果中..."

        return "处理中..."

    async def _create_emitter(
        self, task_id: int, subtask_id: int, callback_info: FeishuCallbackInfo
    ) -> Optional["ResultEmitter"]:
        """Create a streaming emitter for Feishu.

        Currently uses SyncResponseEmitter since Feishu doesn't have
        a streaming card SDK like DingTalk's AI Card.

        Args:
            task_id: Task ID
            subtask_id: Subtask ID
            callback_info: Feishu callback information

        Returns:
            SyncResponseEmitter or None if creation failed
        """
        try:
            from app.services.channels.feishu.sender import FeishuRobotSender
            from app.services.channels.manager import get_channel_manager

            channel_manager = get_channel_manager()
            channel = channel_manager.get_channel(callback_info.channel_id)
            if not channel:
                logger.warning(
                    f"[FeishuCallback] Channel {callback_info.channel_id} not found"
                )
                return None

            # Get app credentials from channel config
            if not hasattr(channel, "config"):
                logger.warning(
                    f"[FeishuCallback] Channel {callback_info.channel_id} has no config"
                )
                return None

            from app.services.channels.emitter import SyncResponseEmitter

            return SyncResponseEmitter()

        except Exception as e:
            logger.exception(
                f"[FeishuCallback] Failed to create emitter for task {task_id}: {e}"
            )
            return None


# Global instance
feishu_callback_service = FeishuCallbackService()

# Register with the callback registry
get_callback_registry().register(ChannelType.FEISHU, feishu_callback_service)
