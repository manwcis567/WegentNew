# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""
Feishu (Lark) IM Channel Integration.

This module provides integration with Feishu/Lark for receiving
and sending messages through the Feishu platform.
"""

from app.services.channels.feishu.callback import (
    FeishuCallbackInfo,
    feishu_callback_service,
)
from app.services.channels.feishu.handler import FeishuChannelHandler
from app.services.channels.feishu.service import FeishuChannelProvider

__all__ = [
    "FeishuChannelProvider",
    "FeishuChannelHandler",
    "FeishuCallbackInfo",
    "feishu_callback_service",
]
