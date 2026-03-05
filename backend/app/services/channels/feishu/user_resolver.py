# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""
Feishu User Resolver.

This module provides functionality to resolve Feishu (Lark) users to Wegent users.
It supports multiple mapping modes: select_user, open_id.
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
USER_MAPPING_MODE_OPEN_ID = "open_id"  # Use open_id as username
USER_MAPPING_MODE_SELECT_USER = "select_user"  # Map all users to a specific user


class FeishuUserResolver:
    """
    Resolves Feishu users to Wegent users.

    Supports:
    1. select_user mode: Map all Feishu users to a configured Wegent user
    2. open_id mode: Use open_id as username to find/create users
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
            user_mapping_mode: User mapping mode (open_id, select_user)
            user_mapping_config: Additional config for user mapping
        """
        self.db = db
        self.user_mapping_mode = user_mapping_mode or USER_MAPPING_MODE_SELECT_USER
        self.user_mapping_config = user_mapping_config or {}

    async def resolve_user(
        self,
        open_id: str,
        sender_name: Optional[str] = None,
    ) -> Optional[User]:
        """
        Resolve a Feishu user to a Wegent user.

        Args:
            open_id: Feishu open_id (app-scoped user identifier)
            sender_name: User's display name (optional)

        Returns:
            User object if found/created, None otherwise
        """
        # Handle select_user mode
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
            # Fall through to default behavior

        # Use open_id as username
        if not open_id:
            logger.warning("[FeishuUserResolver] Cannot resolve user: no open_id")
            return None

        user_name = open_id

        # Find user by username
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
                "[FeishuUserResolver] Found user: open_id=%s -> user_id=%d",
                open_id,
                user.id,
            )
            return user

        # Auto-create user
        logger.info(
            "[FeishuUserResolver] User not found, creating new user: open_id=%s",
            open_id,
        )
        return self._create_user(user_name, sender_name)

    def _create_user(
        self, user_name: str, display_name: Optional[str] = None
    ) -> Optional[User]:
        """
        Create a new user from Feishu information.

        Args:
            user_name: Username (open_id)
            display_name: Display name (optional)

        Returns:
            Created User object or None if failed
        """
        email = f"{user_name}@feishu.user"

        try:
            new_user = User(
                user_name=user_name,
                email=email,
                password_hash=get_password_hash(str(uuid.uuid4())),
                git_info=[],
                is_active=True,
                preferences=json.dumps({}),
                auth_source="feishu",
            )
            self.db.add(new_user)
            self.db.commit()
            self.db.refresh(new_user)

            logger.info(
                "[FeishuUserResolver] Created new user: user_id=%d, user_name=%s",
                new_user.id,
                user_name,
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
