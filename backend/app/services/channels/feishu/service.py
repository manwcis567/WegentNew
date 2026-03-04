# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""
Feishu (Lark) IM Channel Provider.

This module provides the channel provider for Feishu (Lark) integration.
It manages the Feishu event subscription webhook endpoint and message handling.

Feishu uses a Webhook-based event subscription model where:
- Feishu sends messages to our endpoint via HTTP POST
- We acknowledge with 200 OK to confirm receipt
- Messages are processed asynchronously

IM channels are stored as Messager CRD in the kinds table.
"""

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.services.channels.base import BaseChannelProvider

logger = logging.getLogger(__name__)

# CRD kind for IM channels
MESSAGER_KIND = "Messager"
MESSAGER_USER_ID = 0


def _get_channel_default_team_id(channel_id: int) -> Optional[int]:
    """
    Get the current default_team_id for a channel from database.

    Args:
        channel_id: The IM channel ID (Kind.id)

    Returns:
        The default team ID or None
    """
    from app.db.session import SessionLocal
    from app.models.kind import Kind

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
        if channel:
            spec = channel.json.get("spec", {})
            return spec.get("defaultTeamId", 0)
        return None
    finally:
        db.close()


def _get_channel_default_model_name(channel_id: int) -> Optional[str]:
    """
    Get the current default_model_name for a channel from database.

    Args:
        channel_id: The IM channel ID (Kind.id)

    Returns:
        The default model name or None
    """
    from app.db.session import SessionLocal
    from app.models.kind import Kind

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
        if channel:
            spec = channel.json.get("spec", {})
            model_name = spec.get("defaultModelName", "")
            return model_name if model_name else None
        return None
    finally:
        db.close()


def _get_channel_user_mapping_config(channel_id: int) -> Dict[str, Any]:
    """
    Get the user mapping configuration for a channel from database.

    Args:
        channel_id: The IM channel ID (Kind.id)

    Returns:
        Dict with user_mapping_mode and user_mapping_config.
    """
    from app.db.session import SessionLocal
    from app.models.kind import Kind

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
        if channel:
            spec = channel.json.get("spec", {})
            config = spec.get("config", {})
            return {
                "mode": config.get("user_mapping_mode", "select_user"),
                "config": config.get("user_mapping_config"),
            }
        return {"mode": "select_user", "config": None}
    finally:
        db.close()


class FeishuChannelProvider(BaseChannelProvider):
    """
    Feishu IM channel provider using Webhook event subscription.

    Manages the Feishu Webhook endpoint and message processing:
    - Registers webhook endpoint with Feishu
    - Receives messages via HTTP POST
    - Handles message validation (challenge/encrypted messages)
    - Delegates processing to FeishuHandler
    """

    def __init__(self, channel: Any):
        """
        Initialize the Feishu channel provider.

        Args:
            channel: Channel-like object (IMChannelAdapter) with Feishu configuration
        """
        super().__init__(channel)
        self._router: Optional[APIRouter] = None
        self._handler = None
        self._token: Optional[str] = None
        self._aes_key: Optional[str] = None
        self._app_id: Optional[str] = None

    @property
    def app_id(self) -> Optional[str]:
        """Get the Feishu app ID from config."""
        return self.config.get("app_id")

    @property
    def app_secret(self) -> Optional[str]:
        """Get the Feishu app secret from config."""
        return self.config.get("app_secret")

    @property
    def verification_token(self) -> Optional[str]:
        """Get the Feishu verification token from config."""
        return self.config.get("verification_token")

    @property
    def aes_key(self) -> Optional[str]:
        """Get the Feishu AES key (encrypt_key) from config."""
        return self.config.get("aes_key")

    def _is_configured(self) -> bool:
        """Check if Feishu is properly configured."""
        return bool(self.app_id and self.app_secret)

    def _is_encrypted(self) -> bool:
        """Check if encryption is configured."""
        return bool(self.aes_key)

    def _get_request_router(self) -> APIRouter:
        """Get or create the API router for Feishu webhook endpoints."""
        if self._router is None:
            from fastapi import APIRouter

            self._router = APIRouter(prefix="/api/v1/im/feishu", tags=["Feishu"])

            # Register webhook endpoint
            self._router.add_api_route(
                "/webhook",
                self._webhook_handler,
                methods=["GET", "POST"],
                response_class=JSONResponse,
            )

            logger.info(
                "[Feishu] Registered webhook endpoint: /api/v1/im/feishu/webhook"
            )

        return self._router

    async def _webhook_handler(self, request: Request) -> Response:
        """
        Handle incoming Feishu webhook events.

        This is the entry point for all Feishu events:
        - URL verification (GET request with challenge)
        - Message events (POST request with encrypted payload)

        Args:
            request: FastAPI request object

        Returns:
            Response to Feishu
        """
        try:
            if request.method == "GET":
                # URL verification request
                return await self._handle_verification(request)

            elif request.method == "POST":
                # Event callback (message, etc.)
                return await self._handle_event(request)

            return JSONResponse(
                status_code=405, content={"message": "Method Not Allowed"}
            )

        except Exception as e:
            logger.exception("[Feishu] Error handling webhook: %s", e)
            return JSONResponse(
                status_code=500, content={"message": f"Internal Server Error: {e}"}
            )

    async def _handle_verification(self, request: Request) -> Response:
        """
        Handle Feishu URL verification request.

        Feishu sends a GET request to verify our endpoint.
        We need to return the echostr parameter.

        Args:
            request: FastAPI request object

        Returns:
            JSONResponse with echostr
        """
        query_params = dict(request.query_params)
        echostr = query_params.get("echostr", "")

        if not echostr:
            logger.warning("[Feishu] Missing echostr in verification request")
            return JSONResponse(
                status_code=400, content={"message": "Missing echostr parameter"}
            )

        # Return the echostr as required by Feishu verification
        return JSONResponse(content={"echostr": echostr})

    async def _handle_event(self, request: Request) -> Response:
        """
        Handle Feishu event callback.

        Args:
            request: FastAPI request object

        Returns:
            JSONResponse acknowledges receipt
        """
        import json

        # Get raw request body
        body = await request.body()

        # Parse the event data
        # Feishu uses a specific format for encrypted messages
        try:
            data = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as e:
            logger.error("[Feishu] Invalid JSON in webhook: %s", e)
            return JSONResponse(
                status_code=400, content={"message": "Invalid JSON"}
            )

        # Log event for debugging (without sensitive data)
        event_type = data.get("header", {}).get("event_type", "unknown")
        logger.info(
            "[Feishu] Received event: type=%s, app_id=%s",
            event_type,
            data.get("header", {}).get("app_id", "unknown"),
        )

        # Currently we only handle message events
        if event_type == "message":
            # Process message event
            await self._process_message_event(data)
        else:
            logger.debug(
                "[Feishu] Event type not handled: %s", event_type
            )

        # Always return 200 OK to acknowledge receipt
        return JSONResponse(content={"status": 200, "message": "ok"})

    async def _process_message_event(self, event_data: Dict[str, Any]) -> None:
        """
        Process a message event from Feishu.

        Args:
            event_data: The parsed event data from Feishu
        """
        # Extract message content from the event
        # Format: https://open.feishu.cn/document/server-docs/event-subscription-event/introduction
        event = event_data.get("event", {})
        message = event.get("message", {})

        # Get message ID for deduplication
        message_id = message.get("message_id", "")

        if not message_id:
            logger.warning("[Feishu] Message event without message_id")
            return

        # Check if this is a mention event (bot was @mentioned)
        # or a direct message
        message_type = message.get("message_type", "")

        # Only process text messages for now
        if message_type != "text":
            logger.debug(
                "[Feishu] Skipping non-text message type: %s", message_type
            )
            return

        # Extract text content
        content = ""
        if message_type == "text":
            content = message.get("content", "").strip()

        if not content:
            logger.warning("[Feishu] Empty message content")
            return

        # Extract conversation info
        chat_id = message.get("chat_id", "")
        chat_type = message.get("chat_type", "private")

        # Extract sender info
        sender_id = event.get("sender", {}).get("sender_id", {}).get("open_id", "")
        sender_nick = event.get("sender", {}).get("sender_nick", "")

        # Check if bot was mentioned
        at_list = message.get("at_list", [])
        is_mention = any(
            at.get("encekey", "") == "self" for at in at_list
        ) if at_list else False

        # For group chats, we only respond when mentioned
        if chat_type == "group" and not is_mention:
            logger.debug(
                "[Feishu] Skipping group message without mention"
            )
            return

        # Build a message context-like structure for processing
        # We'll pass this to the handler for processing
        message_context = {
            "content": content,
            "sender_id": sender_id,
            "sender_name": sender_nick,
            "conversation_id": chat_id,
            "conversation_type": "group" if chat_type == "group" else "private",
            "is_mention": is_mention,
            "message_id": message_id,
            "raw_message": event_data,
        }

        # Process through the handler
        if self._handler:
            await self._handler.handle_message(message_context)
        else:
            logger.warning(
                "[Feishu] Handler not initialized, message dropped"
            )

    async def start(self) -> bool:
        """
        Start the Feishu channel.

        For Webhook mode, this means registering the webhook endpoint
        with Feishu Open Platform.

        Returns:
            True if started successfully, False otherwise
        """
        if not self._is_configured():
            self._set_error(
                "Feishu not configured: missing app_id or app_secret"
            )
            return False

        if self._is_running:
            logger.warning(
                "[Feishu] Channel %s (id=%d) is already running",
                self.channel_name,
                self.channel_id,
            )
            return True

        try:
            logger.info(
                "[Feishu] Starting channel %s (id=%d)...",
                self.channel_name,
                self.channel_id,
            )

            # Store config values
            self._token = self.verification_token
            self._aes_key = self.aes_key
            self._app_id = self.app_id

            # Create handler with dynamic configuration getters
            channel_id = self.channel_id
            self._handler = WegentFeishuHandler(
                channel_id=channel_id,
                get_default_team_id=lambda: _get_channel_default_team_id(channel_id),
                get_default_model_name=lambda: _get_channel_default_model_name(
                    channel_id
                ),
                get_user_mapping_config=lambda: _get_channel_user_mapping_config(
                    channel_id
                ),
            )

            # Register callback handlers
            # Note: The actual registration with Feishu needs to be done
            # via the Feishu Open Platform console or API
            # This is a placeholder for documentation

            # MARK: The following needs to be configured in Feishu Open Platform:
            # 1. Go to Feishu Open Platform (https://open.feishu.cn)
            # 2. Select your application
            # 3. Go to "Event Subscription" or "Metadata Directory"
            # 4. Configure the webhook URL: {your_domain}/api/v1/im/feishu/webhook
            # 5. Set verification token to match config.verification_token
            # 6. Enable message events (message.at, message)

            self._set_running(True)

            logger.info(
                "[Feishu] Channel %s (id=%d) started successfully",
                self.channel_name,
                self.channel_id,
            )
            logger.info(
                "[Feishu] Webhook URL: /api/v1/im/feishu/webhook"
            )
            logger.info(
                "[Feishu] App ID: %s", self._app_id[:8] + "..." if self._app_id else "N/A"
            )

            return True

        except Exception as e:
            self._set_error(f"Failed to start: {e}")
            self._set_running(False)
            return False

    async def stop(self) -> None:
        """Stop the Feishu channel."""
        if not self._is_running:
            logger.debug(
                "[Feishu] Channel %s (id=%d) is not running",
                self.channel_name,
                self.channel_id,
            )
            return

        logger.info(
            "[Feishu] Stopping channel %s (id=%d)...",
            self.channel_name,
            self.channel_id,
        )
        self._set_running(False)

        self._handler = None
        self._router = None

        logger.info(
            "[Feishu] Channel %s (id=%d) stopped",
            self.channel_name,
            self.channel_id,
        )

    def get_status(self) -> Dict[str, Any]:
        """
        Get the current status of the Feishu provider.

        Returns:
            Dictionary containing status information
        """
        status = super().get_status()
        status["extra_info"] = {
            "app_id": f"{self._app_id[:8]}..." if self._app_id else None,
            "is_encrypted": self._is_encrypted(),
            "default_team_id": self.default_team_id,
        }
        return status

    def get_router(self) -> Optional[APIRouter]:
        """
        Get the API router for Feishu webhook endpoints.

        This router should be mounted in the main FastAPI app.

        Returns:
            APIRouter instance or None
        """
        return self._get_request_router()
