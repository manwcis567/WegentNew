# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""Public callback endpoints for Feishu integrations."""

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request, status

from app.api.endpoints.admin.im_channels import IMChannelAdapter
from app.models.kind import Kind
from app.services.channels import get_channel_manager
from app.services.channels.feishu.handler import FeishuChannelHandler

router = APIRouter(prefix="/channels/feishu", tags=["feishu-callbacks"])
logger = logging.getLogger(__name__)

MESSAGER_KIND = "Messager"
MESSAGER_USER_ID = 0
LEGACY_CARD_ACTION_EVENT = "card.action.trigger"


@router.post("/callbacks/{channel_id}")
async def handle_feishu_callback(channel_id: int, request: Request) -> Dict[str, Any]:
    """Receive legacy Feishu callback payloads that still require HTTP mode."""
    payload = await request.json()
    channel = _get_feishu_channel(channel_id)
    adapter = IMChannelAdapter(channel)

    verification_token = adapter.config.get("verification_token") or adapter.config.get(
        "token"
    )
    if verification_token and payload.get("token") != verification_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid Feishu verification token",
        )

    challenge = payload.get("challenge")
    if isinstance(challenge, str) and challenge:
        return {"challenge": challenge}

    if payload.get("encrypt"):
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Encrypted Feishu callbacks are not supported yet",
        )

    event_data = payload.get("event")
    if not isinstance(event_data, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Feishu callback event payload",
        )

    handler = await _get_feishu_handler(channel_id, adapter)
    handled = await handler.handle_callback_event(LEGACY_CARD_ACTION_EVENT, event_data)
    return {"code": 0, "handled": handled}


def _get_feishu_channel(channel_id: int) -> Kind:
    """Load the Feishu Messager resource for the callback channel ID."""
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        channel = (
            db.query(Kind)
            .filter(
                Kind.id == channel_id,
                Kind.kind == MESSAGER_KIND,
                Kind.user_id == MESSAGER_USER_ID,
                Kind.is_active == True,
            )
            .first()
        )
        if channel is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Feishu channel {channel_id} not found",
            )

        channel_type = channel.json.get("spec", {}).get("channelType")
        if channel_type != "feishu":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Channel {channel_id} is not a Feishu channel",
            )

        return channel
    finally:
        db.close()


async def _get_feishu_handler(
    channel_id: int, adapter: IMChannelAdapter
) -> FeishuChannelHandler:
    """Reuse the live provider handler when available, else construct one on demand."""
    manager = get_channel_manager()
    provider = manager.get_channel(channel_id)
    if provider is not None:
        handler = getattr(provider, "_handler", None)
        if isinstance(handler, FeishuChannelHandler):
            return handler

    return FeishuChannelHandler(
        channel_id=channel_id,
        app_id=(adapter.config.get("app_id") or adapter.config.get("client_id") or ""),
        app_secret=(
            adapter.config.get("app_secret")
            or adapter.config.get("client_secret")
            or ""
        ),
    )
