# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""
Feishu (Lark) IM Channel Streaming Emitter.

This module provides the streaming emitter for Feishu channel integrations.
It sends streaming updates to Feishu conversations.

For Feishu, we send multiple messages as updates since Feishu doesn't
support true streaming in the same way as DingTalk's AI Cards.
"""

import asyncio
import logging
from typing import Any, Dict, Optional

from app.services.execution.emitters import ResultEmitter

logger = logging.getLogger(__name__)


class StreamingResponseEmitter(ResultEmitter):
    """Streaming emitter for Feishu channel.

    Sends streaming updates to Feishu conversations by sending
    multiple messages as updates.

    Note: Feishu doesn't support true streaming like DingTalk's AI Cards,
    so we send multiple messages as progress updates.
    """

    def __init__(
        self,
        sender: Any,
        conversation_id: str,
        min_update_interval: float = 0.5,
    ):
        """Initialize the Feishu streaming emitter.

        Args:
            sender: FeishuSender instance for sending messages
            conversation_id: Target conversation ID
            min_update_interval: Minimum time between updates (seconds)
        """
        self._sender = sender
        self._conversation_id = conversation_id
        self._min_update_interval = min_update_interval
        self._last_update_time = 0.0
        self._current_content = ""
        self._started = False
        self._finished = False
        self._lock = asyncio.Lock()

    async def emit_start(
        self,
        task_id: int,
        subtask_id: int,
        shell_type: Optional[str] = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Emit task start event.

        Args:
            task_id: Task ID
            subtask_id: Subtask ID
            shell_type: Shell type (e.g., "ClaudeCode")
        """
        async with self._lock:
            self._started = True
            self._current_content = ""
            self._last_update_time = 0.0

        # Send start notification
        start_message = f"任务已开始\n\n任务 ID: {task_id}\n状态: 正在执行..."
        await self._sender.send_text(self._conversation_id, start_message)

        logger.info(
            f"[FeishuEmitter] Task {task_id} started, message sent"
        )

    async def emit_chunk(
        self,
        task_id: int,
        subtask_id: int,
        content: str,
        offset: int,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Emit a chunk of content.

        For Feishu, we only send updates if sufficient time has passed
        to avoid spamming the conversation.

        Args:
            task_id: Task ID
            subtask_id: Subtask ID
            content: Content chunk
            offset: Content offset
        """
        import time

        current_time = time.time()

        async with self._lock:
            # Check if we should update
            if current_time - self._last_update_time < self._min_update_interval:
                return

            self._last_update_time = current_time
            self._current_content = content

        # Send the update
        await self._sender.send_text(self._conversation_id, content)

        logger.debug(
            f"[FeishuEmitter] Sent chunk for task {task_id}, content_len={len(content)}"
        )

    async def emit_done(
        self,
        task_id: int,
        subtask_id: int,
        offset: int = 0,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Emit task completion event.

        Args:
            task_id: Task ID
            subtask_id: Subtask ID
            offset: Content offset
        """
        async with self._lock:
            self._finished = True

        # Send completion notification
        done_message = f"任务已完成\n\n任务 ID: {task_id}\n状态: 完成"
        await self._sender.send_text(self._conversation_id, done_message)

        logger.info(f"[FeishuEmitter] Task {task_id} completed")

    async def emit_error(
        self,
        task_id: int,
        subtask_id: int,
        error: str,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Emit error event.

        Args:
            task_id: Task ID
            subtask_id: Subtask ID
            error: Error message
        """
        async with self._lock:
            self._finished = True

        # Send error notification
        error_message = f"任务执行失败\n\n任务 ID: {task_id}\n错误: {error}"
        await self._sender.send_text(self._conversation_id, error_message)

        logger.error(
            f"[FeishuEmitter] Task {task_id} failed: {error}"
        )

    async def wait_for_response(self, timeout: float = 120.0) -> str:
        """Wait for the response to complete.

        For Feishu streaming, we don't return the full content
        since it's already been sent incrementally.

        Args:
            timeout: Timeout in seconds

        Returns:
            Empty string (content already streamed)
        """
        import time

        start_time = time.time()

        while not self._finished:
            if time.time() - start_time > timeout:
                raise asyncio.TimeoutError("Response timeout")
            await asyncio.sleep(0.1)

        return ""

    async def close(self) -> None:
        """Close the emitter."""
        async with self._lock:
            self._started = False
            self._finished = False

    @property
    def is_started(self) -> bool:
        """Check if emitter has started."""
        return self._started

    @property
    def is_finished(self) -> bool:
        """Check if emitter has finished."""
        return self._finished
