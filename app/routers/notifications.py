"""
Notification Router
-------------------
Handles in-app notification retrieval and read/unread actions.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from sqlmodel import Session, select

from app.config import DEMO_MODE
from app.database import get_session
from app.models import User, Notification
from app.routers.auth import get_current_user
from app.services.notification_service import (
    get_unread_count,
    mark_as_read,
    mark_all_as_read,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])

templates = Jinja2Templates(directory="app/templates")


@router.get("", response_class=HTMLResponse)
async def all_notifications_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """Render a page with all notifications for the current user."""
    notifications = db.exec(
        select(Notification)
        .where(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc())
    ).all()

    return templates.TemplateResponse(
        "notifications.html",
        {
            "request": request,
            "current_user": current_user,
            "notifications": notifications,
            "unread_count": get_unread_count(db, current_user.id),
            "latest_notifications": notifications[:10],  # ✅ include latest notifications for navbar
            "flash_messages": [],
            "config": {"DEMO_MODE": DEMO_MODE},
        },
    )


@router.get("/unread-count")
def unread_count(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    return {"unread_count": get_unread_count(db, current_user.id)}


@router.post("/{notification_id}/read")
def read_one(
    notification_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    success = mark_as_read(db, notification_id, current_user.id)
    db.commit()
    return {"success": success}


@router.post("/read-all")
def read_all(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    success = mark_all_as_read(db, current_user.id)
    db.commit()
    return {"success": success}