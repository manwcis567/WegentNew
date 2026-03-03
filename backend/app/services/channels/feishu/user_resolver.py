# SPDX-FileCopyrightText: 2025 Weibo, Inc.
#
# SPDX-License-Identifier: Apache-2.0

"""
Feishu User Resolver.

This module provides functionality to resolve Feishu users to Wegent users.
It supports multiple user mapping modes:
- user_id: Use Feishu user_id as username
- email: Match user by email address
- select_user: Map all Feishu users to a specific Wegent user
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


# User mapping modes for Feishu
USER_MAPPING_MODE_USER_ID = "user_id"  # Use Feishu user_id as username
USER_MAPPING_MODE_EMAIL = "email"  # Match user by email
USER_MAPPING_MODE_SELECT_USER = "select_user"  # Map all users to a specific user


class FeishuUserResolver:
    """
    Resolves Feishu users to Wegent users.

    This class handles the mapping between Feishu user identifiers
    and Wegent user accounts by:
    1. Using channel-configured user mapping mode (select_user, email, user_id)
    2. Falling back to user_id as username
    3. Auto-creating users if not found
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
            user_mapping_mode: User mapping mode (user_id, email, select_user)
            user_mapping_config: Additional config for user mapping (e.g., target_user_id)
        """
        self.db = db
        self.user_mapping_mode = user_mapping_mode or USER_MAPPING_MODE_USER_ID
        self.user_mapping_config = user_mapping_config or {}

    async def resolve_user(
        self,
        sender_id: str,
        sender_name: Optional[str] = None,
        union_id: Optional[str] = None,
    ) -> Optional[User]:
        """
        Resolve a Feishu user to a Wegent user.

        Resolution logic:
        1. If mode is select_user, return the configured target user
        2. If mode is email, try to match by email (from union_id or user_id)
        3. Fall back to user_id as username
        4. Auto-create user if not found

        Args:
            sender_id: Feishu user ID (open_id or user_id)
            sender_name: User's name (optional)
            union_id: Feishu union_id (optional, can be used to get email)

        Returns:
            User object if found/created, None otherwise
        """
        # Step 1: Handle select_user mode - return configured target user
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
                        "[FeishuUserResolver] select_user mode: sender_id=%s -> target_user_id=%d",
                        sender_id,
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

        user_name: Optional[str] = None
        email: Optional[str] = None

        # Step 2: Handle email mode - try to match by email
        # Note: Feishu email would need to be obtained via Feishu API using union_id
        # For now, we require the email to be passed in or stored in a mapping
        if self.user_mapping_mode == USER_MAPPING_MODE_EMAIL:
            # Try to get email from union_id mapping (would need external API call)
            # For now, this mode requires pre-configured email mapping
            if email:
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
                        "[FeishuUserResolver] email mode: sender_id=%s -> email=%s -> user_id=%d",
                        sender_id,
                        email,
                        user.id,
                    )
                    return user
                else:
                    logger.warning(
                        "[FeishuUserResolver] email mode: user not found by email=%s",
                        email,
                    )
            else:
                logger.warning(
                    "[FeishuUserResolver] email mode: no email available for sender_id=%s",
                    sender_id,
                )
            # Fall through to default behavior if email mode fails

        # Step 3: Use user_id as username (default behavior)
        if not user_name:
            if sender_id:
                user_name = sender_id
                logger.debug(
                    "[FeishuUserResolver] Using user_id as username: %s",
                    sender_id,
                )
            else:
                logger.warning(
                    "[FeishuUserResolver] Cannot resolve user: no user_id available",
                )
                return None

        # Step 4: Find user by username
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
                "[FeishuUserResolver] Found user: sender_id=%s -> user_name=%s -> user_id=%d",
                sender_id,
                user_name,
                user.id,
            )
            return user

        # Step 5: Auto-create user
        logger.info(
            "[FeishuUserResolver] User not found, creating new user: user_name=%s",
            user_name,
        )
        return self._create_user(user_name, sender_name, email)

    def _create_user(
        self,
        user_name: str,
        sender_name: Optional[str] = None,
        email: Optional[str] = None,
    ) -> Optional[User]:
        """
        Create a new user from Feishu information.

        Args:
            user_name: Username (Feishu user_id)
            sender_name: Display name from Feishu (optional)
            email: Email address (optional)

        Returns:
            Created User object or None if failed
        """
        # Use default email if not provided
        if not email:
            email = f"{user_name}@feishu.cn"

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
