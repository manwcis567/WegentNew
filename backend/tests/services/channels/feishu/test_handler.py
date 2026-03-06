# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

from unittest.mock import AsyncMock

import pytest

from app.services.channels.feishu.handler import (
    FEISHU_BOT_MENU_CALLBACK_EVENT,
    FEISHU_LEGACY_CARD_CALLBACK_EVENT,
    FeishuChannelHandler,
)


@pytest.fixture
def handler() -> FeishuChannelHandler:
    return FeishuChannelHandler(channel_id=1, app_id="app", app_secret="secret")


@pytest.mark.asyncio
async def test_handle_card_callback_reuses_message_pipeline(
    handler: FeishuChannelHandler,
):
    handler.handle_feishu_event = AsyncMock(return_value=True)

    result = await handler.handle_callback_event(
        FEISHU_LEGACY_CARD_CALLBACK_EVENT,
        {
            "open_message_id": "om_123",
            "operator": {"open_id": "ou_123"},
            "action": {"value": {"command": "/status"}},
        },
    )

    assert result is True
    handler.handle_feishu_event.assert_awaited_once()
    synthetic_event = handler.handle_feishu_event.await_args.args[0]
    assert synthetic_event["message"]["message_id"] == "om_123"
    assert synthetic_event["message"]["chat_id"] == "om_123"
    assert synthetic_event["sender"]["sender_id"]["open_id"] == "ou_123"


@pytest.mark.asyncio
async def test_handle_bot_menu_callback_maps_to_slash_command(
    handler: FeishuChannelHandler,
):
    handler.handle_feishu_event = AsyncMock(return_value=True)

    result = await handler.handle_callback_event(
        FEISHU_BOT_MENU_CALLBACK_EVENT,
        {
            "event_key": "help",
            "operator": {"open_id": "ou_456"},
            "chat_id": "oc_456",
        },
    )

    assert result is True
    handler.handle_feishu_event.assert_awaited_once()
    synthetic_event = handler.handle_feishu_event.await_args.args[0]
    assert synthetic_event["message"]["chat_id"] == "oc_456"
    assert "/help" in synthetic_event["message"]["content"]
