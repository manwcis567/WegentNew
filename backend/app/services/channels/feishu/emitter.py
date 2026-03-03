# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""
Feishu Streaming Response Emitter.

This module provides streaming response functionality for Feishu channel,
sending real-time updates back to the user during task execution.
"""

import json
import logging
from typing import Any, Dict, Optional

from lark_oapi import Client

from app.services.channels.callback import BaseCallbackInfo
from app.services.channels.emitter import SyncResponseEmitter
from app.services.channels.handler import MessageContext

logger = logging.getLogger(__name__)


class FeishuStreamingResponseEmitter(SyncResponseEmitter):
    """
    Streaming response emitter for Feishu.

    Sends real-time updates to Feishu users during task execution.
    Currently implements a simplified version that buffers output
    and sends it as a single message when complete.

    Note: Feishu doesn't have a direct equivalent to DingTalk's AI Card
    for streaming updates. For true streaming, you would need to use
    Feishu's interactive cards with update capabilities.
    """

    def __init__(
        self,
        client: Client,
        message_context: Optional[MessageContext] = None,
        callback_info: Optional[BaseCallbackInfo] = None,
    ):
        """
        Initialize the Feishu streaming emitter.

        Args:
            client: Feishu client for sending messages
            message_context: Original message context (for reply operations)
            callback_info: Callback info (alternative to message_context)
        """
        super().__init__()
        self._client = client
        self._message_context = message_context
        self._callback_info = callback_info
        self._buffer = []
        self._started = False
        self._finished = False
        self._offset = 0

        # Determine receive_id for sending messages
        self._receive_id = None
        if message_context:
            event_data = message_context.extra_data.get("event_data", {})
            sender_info = event_data.get("sender", {})
            self._receive_id = sender_info.get("user_id", message_context.sender_id)
        elif callback_info and hasattr(callback_info, "receive_id"):
            self._receive_id = callback_info.receive_id

    async def emit_start(
        self,
        task_id: int,
        subtask_id: int,
        shell_type: str,
    ) -> None:
        """
        Emit start of task execution.

        Args:
            task_id: Task ID
            subtask_id: Subtask ID
            shell_type: Shell type (e.g., "ClaudeCode")
        """
        self._started = True
        logger.debug(
            "[FeishuEmitter] Task %d started, shell_type=%s",
            task_id,
            shell_type,
        )

    async def emit_chunk(
        self,
        task_id: int,
        subtask_id: int,
        content: str,
        offset: int,
    ) -> None:
        """
        Emit a chunk of output.

        Args:
            task_id: Task ID
            subtask_id: Subtask ID
            content: Output content
            offset: Output offset
        """
        self._buffer.append(content)
        self._offset = offset
        logger.debug(
            "[FeishuEmitter] Task %d chunk: %d chars (offset=%d)",
            task_id,
            len(content),
            offset,
        )

    async def emit_done(
        self,
        task_id: int,
        subtask_id: int,
        offset: int,
    ) -> None:
        """
        Emit task completion.

        Args:
            task_id: Task ID
            subtask_id: Subtask ID
            offset: Final output offset
        """
        self._finished = True

        # Send buffered content as a single message
        if self._buffer:
            full_content = "".join(self._buffer)
            await self._send_message(full_content)
            logger.info(
                "[FeishuEmitter] Task %d completed, sent %d chars",
                task_id,
                len(full_content),
            )
        else:
            # Send empty message if no content
            await self._send_message("任务已完成")
            logger.info("[FeishuEmitter] Task %d completed (no output)", task_id)

    async def _send_message(self, text: str) -> bool:
        """
        Send a text message to Feishu.

        Args:
            text: Message text

        Returns:
            True if sent successfully, False otherwise
        """
        if not self._client or not self._receive_id:
            logger.warning("[FeishuEmitter] Cannot send: client or receive_id missing")
            return False

        try:
            from lark.oapi.api.im.v1 import (
                CreateMessageReqBody,
                CreateMessageRequest,
            )

            content_json = json.dumps({"text": text})

            req = (
                CreateMessageRequest.builder()
                .request_body(
                    CreateMessageReqBody.builder()
                    .receive_id(self._receive_id)
                    .msg_type("text")
                    .content(content_json)
                    .build()
                )
                .build()
            )

            resp = await self._client.im.message.create(req)

            if resp.code == 0:
                logger.debug("[FeishuEmitter] Message sent successfully")
                return True
            else:
                logger.error(
                    "[FeishuEmitter] Failed to send message: code=%s, msg=%s",
                    resp.code,
                    resp.msg,
                )
                return False

        except Exception as e:
            logger.exception("[FeishuEmitter] Failed to send message: %s", e)
            return False

    async def send_text_message(self, text: str) -> bool:
        """
        Send a standalone text message.

        Args:
            text: Message text

        Returns:
            True if sent successfully, False otherwise
        """
        return await self._send_message(text)
