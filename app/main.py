"""
ASPEN Web Application – FastAPI Entry Point
"""

import json
import logging
import os

import sqlalchemy
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import APP_NAME, APP_VERSION, SCHEDULER_INTERVAL_MINUTES, ENGINE_INTERVAL_MINUTES
from app.database import engine, Session
from app.models import User
from app.services.auth import hash_password
from app.services.engine_service import run_engine_service, _engine_lock
from app.services.sms import send_sms
from app.services.expiry_service import (
    expire_stale_matches,
    expire_acceptance_deadlines,
    expire_dispatch_deadlines,
    expire_delivery_deadlines,
    expire_spoiled_lots,
)
from app.services.trust_service import update_vendor_trust_scores
from sqlmodel import SQLModel, select

logger = logging.getLogger("aspen.main")

# Create the FastAPI app instance
app = FastAPI(
    title=APP_NAME,
    description="Algorithmic Spoilage‑Prevention Engine",
    version=APP_VERSION,
    docs_url="/docs",
    redoc_url=None,
)

# ---------------------------------------------------------------------------
# Global exception handlers
# ---------------------------------------------------------------------------
@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    """Return consistent 422 for manual ValueError from routes/services."""
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(sqlalchemy.exc.IntegrityError)
async def integrity_error_handler(request: Request, exc):
    """Map database constraint violations to 409."""
    return JSONResponse(status_code=409, content={"detail": "Database constraint violation."})


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    """Log unexpected errors and return clean 500 without leaking details."""
    logger.exception("Unhandled exception", exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


# ---------------------------------------------------------------------------
# CORS (allow all origins during development)
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Mount static files directory (for images, CSS, JS)
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Module-level scheduler reference.
# This keeps a strong reference to the BackgroundScheduler so it is
# not garbage-collected after the startup function returns.
scheduler_instance = None


# ---------------------------------------------------------------------------
# Create database tables on startup (if they don't exist) and start scheduler
# ---------------------------------------------------------------------------
@app.on_event("startup")
def on_startup():
    global scheduler_instance

    # Create all tables defined by SQLModel models
    import app.models  # noqa: F401
    SQLModel.metadata.create_all(engine)

    # --- Auto-create admin users from environment variable (if any) ---
    admin_users_json = os.getenv("ADMIN_USERS")
    if admin_users_json:
        try:
            admins = json.loads(admin_users_json)
        except json.JSONDecodeError:
            print("WARNING: ADMIN_USERS env var is not valid JSON. Skipping admin creation.")
            admins = []

        with Session(engine) as db:
            for entry in admins:
                phone = entry.get("phone", "").replace(" ", "")
                if not phone:
                    continue
                existing = db.exec(select(User).where(User.phone == phone)).first()
                if existing:
                    continue
                admin = User(
                    name=entry.get("name", "Admin"),
                    phone=phone,
                    password_hash=hash_password(entry.get("password", "changeme")),
                    role="admin",
                    town=entry.get("town", ""),
                    region=entry.get("region", ""),
                    district=entry.get("district", ""),
                    is_active=True,
                )
                db.add(admin)
            db.commit()

    # --- Scheduler ---
    scheduler = BackgroundScheduler()
    scheduler_instance = scheduler

    # ---------- 1. High‑frequency expiry/watchdog tasks (every SCHEDULER_INTERVAL_MINUTES) ----------
    def _expiry_tasks():
        with _engine_lock:
            expire_stale_matches()
            expire_acceptance_deadlines()
            expire_dispatch_deadlines()
            expire_delivery_deadlines()
            expire_spoiled_lots()

    scheduler.add_job(
        _expiry_tasks,
        'interval',
        minutes=SCHEDULER_INTERVAL_MINUTES,
        id='expiry_tasks',
        replace_existing=True,
    )

    # ---------- 2. Lower‑frequency engine run (every ENGINE_INTERVAL_MINUTES) ----------
    def _engine_task():
        with _engine_lock:
            db = Session(engine)
            try:
                summary, sms_list = run_engine_service(db)
                # Send SMS notifications for all new matches
                for item in sms_list:
                    send_sms(
                        phone=item["phone"],
                        message=item["message"],
                        db_session=db,
                    )
            finally:
                db.close()

    scheduler.add_job(
        _engine_task,
        'interval',
        minutes=ENGINE_INTERVAL_MINUTES,
        id='engine_task',
        replace_existing=True,
    )

    # ---------- 3. Trust score evaluation (every 60 minutes) ----------
    scheduler.add_job(
        update_vendor_trust_scores,
        'interval',
        minutes=60,
        id='trust_update',
        replace_existing=True,
    )

    scheduler.start()


@app.on_event("shutdown")
def on_shutdown():
    """Gracefully stop the scheduler when the app is shutting down."""
    global scheduler_instance
    if scheduler_instance:
        scheduler_instance.shutdown(wait=False)
        scheduler_instance = None


# ---------------------------------------------------------------------------
# Include routers
# ---------------------------------------------------------------------------
from app.routers import auth, farmers, vendors, admin, web, matches, notifications

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(farmers.router, prefix="/farmers", tags=["farmers"])
app.include_router(vendors.router, prefix="/vendors", tags=["vendors"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])
app.include_router(matches.router)        # /matches endpoints
app.include_router(notifications.router)  # /notifications endpoints
app.include_router(web.router)            # HTML pages

# ---------------------------------------------------------------------------
# Health check endpoint
# ---------------------------------------------------------------------------
@app.get("/health")
def health_check():
    return {"status": "ok", "version": APP_VERSION}