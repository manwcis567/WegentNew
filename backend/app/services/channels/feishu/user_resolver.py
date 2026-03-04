# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""
Feishu (Lark) User Resolver.

This module provides functionality to resolve Feishu users to Wegent users.
它使用可插拔的用户映射接口进行企业用户解析。如果没有注册映射器或映射失败，则回退到 open_id。
"""

import json
import logging
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.core.security import get_password_hash
from app.models.user import User
from app.services.k_batch import apply_default_resources_sync

logger = logging.getLogger(__name__)


# User mapping modes
USER_MAPPING_MODE_OPEN_ID = "open_id"  # Use open_id as username (original behavior)
USER_MAPPING_MODE_EMAIL = "email"  # Match user by email
USER_MAPPING_MODE_SELECT_USER = "select_user"  # Map all users to a specific user


class FeishuUserResolver:
    """
    Resolves Feishu (Lark) users to Wegent users.

    This class handles the mapping between Feishu user identifiers
    and Wegent user accounts by:
    1. Using channel-configured user mapping mode (select_user, email, open_id)
    2. Using the registered user mapper (for enterprise deployments)
    3. Falling back to open_id as username
    4. Auto-creating users if not found
    """

    def __init__(
        self,
        db: Session,
        user_mapping_mode: Optional[str] = None,
        user_mapping_config: Optional[dict] = None,
    ):
        """
        Initialize the resolver.

        Args:
            db: Database session
            user_mapping_mode: User mapping mode (open_id, email, select_user)
            user_mapping_config: Additional config for user mapping (e.g., target_user_id)
        """
        self.db = db
        self.user_mapping_mode = user_mapping_mode or USER_MAPPING_MODE_OPEN_ID
        self.user_mapping_config = user_mapping_config or {}

    async def resolve_user(
        self,
        open_id: str,
        nick_name: Optional[str] = None,
        email: Optional[str] = None,
    ) -> Optional[User]:
        """
        Resolve a Feishu user to a Wegent user.

        Resolution logic:
        1. If mode is select_user, return the configured target user
        2. If mode is email, try to match by email
        3. Fall back to open_id as username
        4. Auto-create user if not found

        Args:
            open_id: Feishu user's open_id
            nick_name: User's nickname (optional)
            email: User's email (optional)

        Returns:
            User object if found/created, None otherwise
        """
        # Step 0: Handle select_user mode - return configured target user
        if self.user_mapping_mode == USER_MAPPING_MODE_SELECT_USER:
            target_user_id = self.user_mapping_config.get("target_user_id")
            if target_user_id:
                user = (
                    self.db.query(User)
                    .filter(
                        User.id == target_user_id,
                        User.is_active == True,
                    )
                    .first()
                )
                if user:
                    logger.info(
                        "[FeishuUserResolver] select_user mode: open_id=%s -> target_user_id=%d",
                        open_id,
                        target_user_id,
                    )
                    return user
                else:
                    logger.warning(
                        "[FeishuUserResolver] select_user mode: target user not found, "
                        "target_user_id=%d",
                        target_user_id,
                    )
            else:
                logger.warning(
                    "[FeishuUserResolver] select_user mode: no target_user_id configured"
                )
            # Fall through to default behavior if select_user fails

        # Step 1: Handle email mode - try to find user by email
        if self.user_mapping_mode == USER_MAPPING_MODE_EMAIL and email:
            user = (
                self.db.query(User)
                .filter(
                    User.email == email,
                    User.is_active == True,
                )
                .first()
            )
            if user:
                logger.info(
                    "[FeishuUserResolver] email mode: open_id=%s -> email=%s -> user_id=%d",
                    open_id,
                    email,
                    user.id,
                )
                return user
            else:
                logger.warning(
                    "[FeishuUserResolver] email mode: user not found by email=%s",
                    email,
                )
            # Fall through to default behavior if email mode fails

        # Step 2: Fall back to open_id as username
        # Feishu's open_id is already a stable identifier
        user_name = open_id
        logger.debug(
            "[FeishuUserResolver] Using open_id as username: %s",
            open_id,
        )

        # Step 3: Find user by username (open_id)
        user = (
            self.db.query(User)
            .filter(
                User.user_name == user_name,
                User.is_active == True,
            )
            .first()
        )

        if user:
            logger.info(
                "[FeishuUserResolver] Found user: open_id=%s -> user_name=%s -> user_id=%d",
                open_id,
                user_name,
                user.id,
            )
            return user

        # Step 4: Auto-create user
        logger.info(
            "[FeishuUserResolver] User not found, creating new user: user_name=%s",
            user_name,
        )
        return self._create_user(user_name, email, nick_name)

    def _create_user(
        self,
        user_name: str,
        email: Optional[str] = None,
        nick_name: Optional[str] = None,
    ) -> Optional[User]:
        """
        Create a new user from Feishu information.

        Args:
            user_name: Username (Feishu open_id)
            email: Email address (optional)
            nick_name: Nickname (optional, used for display name)

        Returns:
            Created User object or None if failed
        """
        # Use default email if not provided
        if not email:
            email = f"{user_name}@feishu.cn"

        try:
            # Use nick_name as display_name if available
            display_name = nick_name if nick_name else user_name

            new_user = User(
                user_name=user_name,
                email=email,
                password_hash=get_password_hash(str(uuid.uuid4())),
                git_info=[],
                is_active=True,
                preferences=json.dumps({}),
                auth_source="feishu",
                display_name=display_name,
            )
            self.db.add(new_user)
            self.db.commit()
            self.db.refresh(new_user)

            logger.info(
                "[FeishuUserResolver] Created new user: user_id=%d, user_name=%s, email=%s",
                new_user.id,
                user_name,
                email,
            )

            # Apply default resources for new user
            try:
                apply_default_resources_sync(new_user.id)
                logger.info(
                    "[FeishuUserResolver] Applied default resources for user %d",
                    new_user.id,
                )
            except Exception as e:
                logger.warning(
                    "[FeishuUserResolver] Failed to apply default resources for user %d: %s",
                    new_user.id,
                    e,
                )

            return new_user

        except Exception as e:
            logger.error(
                "[FeishuUserResolver] Failed to create user: user_name=%s, error=%s",
                user_name,
                e,
            )
            self.db.rollback()
            return None
