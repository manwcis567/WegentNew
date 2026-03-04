# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""
Feishu (Lark) IM Channel Package.

This package provides the channel provider for Feishu (Lark) integration.
Feishu uses event subscriptionWebhook mode with long polling.

This file is part of the Wegent IM channel system.
"""

from app.services.channels.feishu.service import FeishuChannelProvider
from app.services.channels.feishu.handler import FeishuChannelHandler, WegentFeishuHandler
from app.services.channels.feishu.callback import FeishuCallbackInfo, feishu_callback_service
from app.services.channels.feishu.emitter import StreamingResponseEmitter
from app.services.channels.feishu.sender import FeishuSender
from app.services.channels.feishu.user_resolver import FeishuUserResolver

__all__ = [
    'FeishuChannelProvider',
    'FeishuChannelHandler',
    'WegentFeishuHandler',
    'FeishuCallbackInfo',
    'feishu_callback_service',
    'StreamingResponseEmitter',
    'FeishuSender',
    'FeishuUserResolver',
]
