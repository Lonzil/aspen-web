"""
Notification Service
--------------------
Handles creation, retrieval, and read/unread state of in‑app notifications.

Notifications are created **within the same database transaction** as the
event that triggered them. They should never be committed separately if the
parent operation fails; callers are expected to call `db.commit()` once after
all related changes are staged.
"""

import logging
from typing import Optional

from sqlmodel import Session, select, func

from app.models import Notification, NotificationType

logger = logging.getLogger("aspen.notification_service")


def create_notification(
    db: Session,
    user_id: int,
    message: str,
    type: NotificationType = NotificationType.INFO,
) -> Optional[Notification]:
    """
    Create a notification for the given user.

    The notification is added to the session but **not committed** here.
    The caller should commit the session at the same time as the triggering
    event to ensure atomicity.

    Returns the created Notification object, or None if creation fails.
    """
    try:
        notification = Notification(
            user_id=user_id,
            message=message,
            type=type,
        )
        db.add(notification)
        return notification
    except Exception:
        logger.exception("Failed to create notification for user %d", user_id)
        return None


def get_unread_count(db: Session, user_id: int) -> int:
    """
    Return the number of unread notifications for a user.
    """
    try:
        count = db.exec(
            select(func.count(Notification.id))
            .where(Notification.user_id == user_id, Notification.is_read == False)
        ).one()
        return int(count or 0)
    except Exception:
        logger.exception("Failed to get unread notification count for user %d", user_id)
        return 0


def get_latest_notifications(db: Session, user_id: int, limit: int = 10):
    """
    Return the latest notifications for a user, ordered by most recent first.
    """
    try:
        notifications = db.exec(
            select(Notification)
            .where(Notification.user_id == user_id)
            .order_by(Notification.created_at.desc())
            .limit(limit)
        ).all()
        return notifications
    except Exception:
        logger.exception("Failed to get latest notifications for user %d", user_id)
        return []


def mark_as_read(db: Session, notification_id: int, user_id: int) -> bool:
    """
    Mark a single notification as read if it belongs to the given user.
    Returns True if successful, False otherwise.
    """
    try:
        notification = db.exec(
            select(Notification)
            .where(
                Notification.id == notification_id,
                Notification.user_id == user_id,
            )
        ).first()

        if not notification:
            return False

        notification.is_read = True
        db.add(notification)
        return True
    except Exception:
        logger.exception("Failed to mark notification %d as read", notification_id)
        return False


def mark_all_as_read(db: Session, user_id: int) -> bool:
    """
    Mark all notifications for a user as read.
    Returns True if successful, False otherwise.
    """
    try:
        notifications = db.exec(
            select(Notification)
            .where(Notification.user_id == user_id, Notification.is_read == False)
        ).all()

        for notification in notifications:
            notification.is_read = True
            db.add(notification)

        return True
    except Exception:
        logger.exception("Failed to mark all notifications as read for user %d", user_id)
        return False