# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""
Feishu Robot Message Sender.

This module provides functionality to proactively send messages to users
via Feishu robot API. Used for subscription notifications and other
push scenarios where there's no incoming message to reply to.

API Reference:
- Send message: POST /open-apis/im/v1/messages
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class FeishuRobotSender:
    """Sender for proactively sending Feishu robot messages.

    This class uses Feishu's im/v1/messages API to send messages
    to users without requiring an incoming message context.
    """

    BASE_URL = "https://open.feishu.cn"

    def __init__(self, app_id: str, app_secret: str):
        """Initialize the sender.

        Args:
            app_id: Feishu app ID
            app_secret: Feishu app secret
        """
        self.app_id = app_id
        self.app_secret = app_secret
        self._tenant_access_token: Optional[str] = None

    async def _get_tenant_access_token(self) -> str:
        """Get tenant access token for Feishu API.

        Returns:
            Tenant access token string

        Raises:
            Exception: If token fetch fails
        """
        url = f"{self.BASE_URL}/open-apis/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": self.app_id,
            "app_secret": self.app_secret,
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url, json=payload, headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            data = response.json()

            if data.get("code") != 0:
                error_msg = data.get("msg", "Unknown error")
                raise Exception(f"Failed to get tenant access token: {error_msg}")

            token = data.get("tenant_access_token")
            if not token:
                raise Exception("Missing tenant_access_token in response")

            return token

    async def send_text_message(
        self,
        receive_id: str,
        content: str,
        receive_id_type: str = "open_id",
    ) -> Dict[str, Any]:
        """Send text message to a user.

        Args:
            receive_id: Receiver ID (open_id, user_id, union_id, email, or chat_id)
            content: Text message content
            receive_id_type: Type of receive_id (open_id, user_id, union_id, email, chat_id)

        Returns:
            API response dict
        """
        msg_content = json.dumps({"text": content}, ensure_ascii=False)
        return await self._send_message(
            receive_id=receive_id,
            msg_type="text",
            content=msg_content,
            receive_id_type=receive_id_type,
        )

    async def send_text_to_chat(
        self,
        chat_id: str,
        content: str,
    ) -> Dict[str, Any]:
        """Send text message to a chat/group.

        Args:
            chat_id: Chat ID
            content: Text message content

        Returns:
            API response dict
        """
        return await self.send_text_message(
            receive_id=chat_id,
            content=content,
            receive_id_type="chat_id",
        )

    async def reply_text_message(
        self,
        message_id: str,
        content: str,
    ) -> Dict[str, Any]:
        """Reply to a message with text.

        Args:
            message_id: The message ID to reply to
            content: Text message content

        Returns:
            API response dict
        """
        try:
            token = await self._get_tenant_access_token()
            url = f"{self.BASE_URL}/open-apis/im/v1/messages/{message_id}/reply"

            msg_content = json.dumps({"text": content}, ensure_ascii=False)
            payload = {
                "msg_type": "text",
                "content": msg_content,
            }

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
                response.raise_for_status()
                data = response.json()

                if data.get("code") != 0:
                    error_msg = data.get("msg", "Unknown error")
                    logger.error(
                        f"[FeishuSender] Reply failed: {error_msg}"
                    )
                    return {"success": False, "error": error_msg}

                logger.info(
                    f"[FeishuSender] Reply sent successfully to message {message_id}"
                )
                return {"success": True, "result": data.get("data", {})}

        except Exception as e:
            logger.error(f"[FeishuSender] Error replying to message: {e}")
            return {"success": False, "error": str(e)}

    async def _send_message(
        self,
        receive_id: str,
        msg_type: str,
        content: str,
        receive_id_type: str = "open_id",
    ) -> Dict[str, Any]:
        """Send message via Feishu API.

        Args:
            receive_id: Receiver ID
            msg_type: Message type (text, post, interactive, etc.)
            content: Message content (JSON string)
            receive_id_type: Type of receive_id

        Returns:
            API response dict
        """
        try:
            token = await self._get_tenant_access_token()

            url = f"{self.BASE_URL}/open-apis/im/v1/messages"
            params = {"receive_id_type": receive_id_type}
            payload = {
                "receive_id": receive_id,
                "msg_type": msg_type,
                "content": content,
            }

            logger.info(
                f"[FeishuSender] Sending {msg_type} message to {receive_id_type}={receive_id}"
            )

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    params=params,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
                response.raise_for_status()
                data = response.json()

                if data.get("code") != 0:
                    error_msg = data.get("msg", "Unknown error")
                    logger.error(
                        f"[FeishuSender] Send failed: {error_msg}"
                    )
                    return {"success": False, "error": error_msg}

                logger.info(
                    f"[FeishuSender] Message sent successfully, "
                    f"message_id={data.get('data', {}).get('message_id')}"
                )
                return {"success": True, "result": data.get("data", {})}

        except httpx.HTTPStatusError as e:
            error_data = {}
            try:
                error_data = e.response.json()
            except Exception:
                pass

            error_code = error_data.get("code", "HTTP_ERROR")
            error_msg = error_data.get("msg", str(e))

            logger.error(
                f"[FeishuSender] HTTP error sending message: {error_code} - {error_msg}"
            )
            return {"success": False, "error": f"{error_code}: {error_msg}"}

        except Exception as e:
            logger.error(f"[FeishuSender] Error sending message: {e}")
            return {"success": False, "error": str(e)}
