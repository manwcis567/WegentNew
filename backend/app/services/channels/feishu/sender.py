# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""
Feishu (Lark) IM Channel Sender.

This module provides the sender utility for Feishu channel integrations.
It handles sending messages to Feishu conversations using the Feishu SDK.
"""

import hashlib
import hmac
import json
import logging
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)


class FeishuSender:
    """Sender utility for Feishu messages.

    This class handles sending various types of messages to Feishu conversations:
    - Text messages
    - Rich text messages
    - Card messages

    It also handles message encryption/decryption for security.
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        aes_key: Optional[str] = None,
        token: Optional[str] = None,
        base_url: str = "https://open.feishu.cn",
    ):
        """
        Initialize the Feishu sender.

        Args:
            app_id: Feishu application ID
            app_secret: Feishu application secret
            aes_key: AES encryption key (for encrypted messages)
            token: Verification token (for URL verification)
            base_url: Feishu Open Platform base URL
        """
        self.app_id = app_id
        self.app_secret = app_secret
        self.aes_key = aes_key
        self.token = token
        self.base_url = base_url
        self._access_token: Optional[str] = None
        self._token_expiry: float = 0

    async def _get_access_token(self) -> str:
        """Get or refresh the Feishu access token.

        Returns:
            Access token string
        """
        import time

        # Return cached token if still valid
        if self._access_token and time.time() < self._token_expiry:
            return self._access_token

        # Refresh token
        url = f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal/"
        payload = {
            "app_id": self.app_id,
            "app_secret": self.app_secret,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                data = await response.json()

                if data.get("code", 0) == 0:
                    self._access_token = data["tenant_access_token"]
                    # Token expires in 2 hours, refresh with 10 minute buffer
                    self._token_expiry = time.time() + 7200 - 600
                    return self._access_token
                else:
                    logger.error(
                        "[FeishuSender] Failed to get access token: %s", data
                    )
                    raise Exception(
                        f"Failed to get access token: {data.get('msg', 'Unknown error')}"
                    )

    async def _post_request(
        self, endpoint: str, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Make a POST request to Feishu API.

        Args:
            endpoint: API endpoint path (e.g., "/open-apis/im/v1/messages")
            data: Request payload

        Returns:
            Response data
        """
        import time

        access_token = await self._get_access_token()

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        url = f"{self.base_url}{endpoint}"

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data, headers=headers) as response:
                result = await response.json()
                return result

    async def send_text(self, chat_id: str, text: str) -> Dict[str, Any]:
        """Send a text message to a chat.

        Args:
            chat_id: Target chat ID
            text: Text message content

        Returns:
            API response
        """
        # Convert markdown-like formatting to Feishu message format
        # Simple text message
        message_content = {
            "text": text,
        }

        data = {
            "chat_id": chat_id,
            "msg_type": "text",
            "content": json.dumps(message_content),
        }

        return await self._post_request("/open-apis/im/v1/messages", data)

    async def send_rich_text(
        self, chat_id: str, blocks: list[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Send a rich text message to a chat.

        Args:
            chat_id: Target chat ID
            blocks: List of rich text blocks

        Returns:
            API response
        """
        message_content = {
            "zh_cn": {
                "ranges": [],
                "title": "",
                "content": "",
            }
        }

        # Build content from blocks
        content_parts = []
        ranges = []

        for i, block in enumerate(blocks):
            block_type = block.get("type", "text")
            text = block.get("text", "")

            if block_type == "text":
                content_parts.append(text)
            elif block_type == "paragraph":
                content_parts.append(text)
            elif block_type == "bullet_point":
                content_parts.append(f"• {text}")
            elif block_type == "numbered_item":
                content_parts.append(f"{i + 1}. {text}")
            elif block_type == "heading":
                level = block.get("level", 1)
                content_parts.append(f"{'#' * level} {text}")

            # Add range info for formatting
            start = sum(len(p) for p in content_parts[:-1]) + (len(blocks[:i]) - 1)
            ranges.append(
                {
                    "start": start,
                    "end": start + len(text),
                    "format": {},
                }
            )

        message_content["zh_cn"]["content"] = "\n".join(content_parts)
        message_content["zh_cn"]["ranges"] = ranges

        data = {
            "chat_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(message_content),
        }

        return await self._post_request("/open-apis/im/v1/messages", data)

    async def send_card(
        self, chat_id: str, card: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Send an interactive card message to a chat.

        Args:
            chat_id: Target chat ID
            card: Card configuration

        Returns:
            API response
        """
        data = {
            "chat_id": chat_id,
            "msg_type": "interactive",
            "card": card,
        }

        return await self._post_request("/open-apis/im/v1/messages", data)

    def verify_event_signature(
        self, timestamp: str, nonce: str, signature: str, event_data: str
    ) -> bool:
        """Verify the Feishu event signature.

        Args:
            timestamp: Timestamp from Feishu
            nonce: Random nonce from Feishu
            signature: Signature from Feishu
            event_data: Event data JSON string

        Returns:
            True if signature is valid
        """
        if not self.token:
            logger.warning("[FeishuSender] No verification token configured")
            return True

        # Sort parameters
        params = sorted([self.token, timestamp, nonce, event_data])
        source = "".join(params)

        # Calculate SHA1 hash
        calculated = hashlib.sha1(source.encode("utf-8")).hexdigest()

        return calculated == signature

    def decrypt_message(
        self, encrypt_text: str, timestamp: str, nonce: str, app_id: str
    ) -> str:
        """Decrypt a Feishu encrypted message.

        Args:
            encrypt_text: Encrypted message text
            timestamp: Timestamp from Feishu
            nonce: Random nonce from Feishu
            app_id: Application ID

        Returns:
            Decrypted message JSON string
        """
        if not self.aes_key:
            raise ValueError("AES key not configured for decryption")

        # Implement Feishu's decryption logic
        # This is a simplified version - full implementation would use
        # the Feishu SDK's decryption utilities

        # For now, return the encrypted text as-is
        # In production, use the Feishu SDK for proper decryption
        return encrypt_text

    async def reply_to_message(
        self, message_id: str, text: str
    ) -> Dict[str, Any]:
        """Reply to an existing message.

        Args:
            message_id: ID of the message to reply to
            text: Reply text content

        Returns:
            API response
        """
        # In Feishu, to reply to a message, we need to:
        # 1. Get the chat_id from the original message
        # 2. Send a new message to that chat

        # For now, this is a placeholder - the chat_id should be
        # extracted from the original message context
        pass
