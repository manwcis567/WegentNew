# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

import pytest

from app.services.channels.feishu.emitter import StreamingResponseEmitter


@pytest.mark.asyncio
async def test_emit_done_sends_markdown_message():
    calls = {}

    class _MockSender:
        async def send_markdown_message(self, chat_id, markdown):
            calls["chat_id"] = chat_id
            calls["markdown"] = markdown
            return {"success": True}

    emitter = StreamingResponseEmitter(sender=_MockSender(), chat_id="oc_123")
    await emitter.emit(
        SimpleNamespace(
            type="response.output_text.delta",
            data={"delta": "## Title\n\n- item"},
            task_id=1,
            subtask_id=2,
        )
    )
    await emitter.emit(
        SimpleNamespace(type="response.completed", data={}, task_id=1, subtask_id=2)
    )

    assert calls["chat_id"] == "oc_123"
    assert calls["markdown"] == "## Title\n\n- item"


@pytest.mark.asyncio
async def test_emit_error_sends_markdown_message():
    calls = {}

    class _MockSender:
        async def send_markdown_message(self, chat_id, markdown):
            calls["chat_id"] = chat_id
            calls["markdown"] = markdown
            return {"success": True}

    emitter = StreamingResponseEmitter(sender=_MockSender(), chat_id="oc_456")
    await emitter.emit_error(task_id=1, subtask_id=2, error="boom")

    assert calls["chat_id"] == "oc_456"
    assert calls["markdown"] == "**执行失败**\n\nboom"
