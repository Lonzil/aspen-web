"""
ASPEN Database Setup

Creates the SQLAlchemy engine with WAL mode for concurrent access
and ensures foreign keys are enforced.  Provides a FastAPI dependency
that yields a database session per request.
"""

from sqlmodel import create_engine, Session
from sqlalchemy import event

from app.config import DATABASE_URL

# ---------------------------------------------------------------------------
# Engine with WAL mode & foreign keys enabled
# ---------------------------------------------------------------------------
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # required for SQLite
)

# Enable Write-Ahead Logging (WAL) for concurrent reads/writes
with engine.connect() as conn:
    conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
    # Foreign key PRAGMA is applied per connection below.

# ---------------------------------------------------------------------------
# Ensure foreign keys are enforced on every new SQLite connection
# ---------------------------------------------------------------------------
@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.close()


# ---------------------------------------------------------------------------
# FastAPI session dependency
# ---------------------------------------------------------------------------
def get_session():
    """
    Yield a database session and ensure it is closed after the request.
    Use this as a FastAPI dependency in your routers:
        @router.get("/something")
        def something(session: Session = Depends(get_session)):
            ...
    """
    with Session(engine) as session:
        yield session