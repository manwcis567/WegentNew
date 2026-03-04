# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""
Feishu (Lark) callback service for device/cloud task execution.

This module provides functionality to send streaming updates and task completion
results back to Feishu when tasks are executed on devices or cloud executors.

Supports:
- Streaming progress updates via text messages
- Task completion notifications
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

    # Serialized incoming_message data for reply
    incoming_message_data: Optional[Dict[str, Any]] = None

    def __init__(
        self,
        channel_id: int,
        conversation_id: str,
        incoming_message_data: Optional[Dict[str, Any]] = None,
    ):
        """Initialize FeishuCallbackInfo.

        Args:
            channel_id: Feishu channel ID for getting client
            conversation_id: Feishu conversation ID
            incoming_message_data: Serialized incoming_message data for reply
        """
        super().__init__(
            channel_type=ChannelType.FEISHU,
            channel_id=channel_id,
            conversation_id=conversation_id,
        )
        self.incoming_message_data = incoming_message_data

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Redis storage."""
        data = super().to_dict()
        data.update(
            {
                "incoming_message_data": self.incoming_message_data,
            }
        )
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FeishuCallbackInfo":
        """Create from dictionary."""
        return cls(
            channel_id=data.get("channel_id", 0),
            conversation_id=data.get("conversation_id", ""),
            incoming_message_data=data.get("incoming_message_data"),
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

        Only returns the most recent thinking step to avoid accumulation.

        Args:
            thinking: Thinking content from AI response

        Returns:
            Formatted thinking text
        """
        if not thinking:
            return ""

        if not isinstance(thinking, list) or len(thinking) == 0:
            return ""

        # Get the latest thinking step
        latest = thinking[-1]
        if not isinstance(latest, dict):
            return str(latest) if latest else ""

        details = latest.get("details", {})
        if not isinstance(details, dict):
            return ""

        detail_type = details.get("type", "")

        # Format based on type
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
            # Assistant message - check content for tool_use or text
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
                                    # Truncate if too long
                                    if len(text) > 100:
                                        return f"{text[:100]}..."
                                    return text
            return "思考中..."

        elif detail_type == "result":
            return "生成结果中..."

        # Default: return a generic message
        return "处理中..."

    async def _create_emitter(
        self, task_id: int, subtask_id: int, callback_info: FeishuCallbackInfo
    ) -> Optional["ResultEmitter"]:
        """Create a streaming emitter for Feishu.

        Args:
            task_id: Task ID
            subtask_id: Subtask ID
            callback_info: Feishu callback information

        Returns:
            StreamingResponseEmitter or None if creation failed
        """
        try:
            # Get Feishu channel to access the sender
            from app.services.channels.manager import get_channel_manager

            channel_manager = get_channel_manager()
            channel = channel_manager.get_channel(callback_info.channel_id)
            if not channel:
                logger.warning(
                    f"[FeishuCallback] Channel {callback_info.channel_id} not found"
                )
                return None

            # Get the sender from the channel handler
            if not hasattr(channel, "_handler") or not channel._handler:
                logger.warning(
                    f"[FeishuCallback] Channel {callback_info.channel_id} has no handler"
                )
                return None

            sender = channel._handler._channel_handler._sender
            if not sender:
                logger.warning(
                    f"[FeishuCallback] Channel {callback_info.channel_id} has no sender"
                )
                return None

            # Create new emitter
            from app.services.channels.feishu.emitter import StreamingResponseEmitter

            emitter = StreamingResponseEmitter(
                sender=sender,
                conversation_id=callback_info.conversation_id,
            )

            return emitter

        except Exception as e:
            logger.exception(
                f"[FeishuCallback] Failed to create emitter for task {task_id}: {e}"
            )
            return None


# Global instance
feishu_callback_service = FeishuCallbackService()

# Register with the callback registry
get_callback_registry().register(ChannelType.FEISHU, feishu_callback_service)
