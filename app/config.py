"""
ASPEN Web Application – Central Configuration.

All tunable parameters and secrets are loaded from environment
variables (via a .env file).  This keeps the engine‑specific
config in one place and makes the web layer easy to configure.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# General App Settings
# ---------------------------------------------------------------------------
APP_NAME: str = "ASPEN"
APP_VERSION: str = "1.0.0"
DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///aspen.db")

# ---------------------------------------------------------------------------
# JWT Authentication
# ---------------------------------------------------------------------------
JWT_SECRET: str = os.getenv("JWT_SECRET", "change-me-in-production")
JWT_ALGORITHM: str = "HS256"
JWT_EXPIRATION_HOURS: int = int(os.getenv("JWT_EXPIRATION_HOURS", "24"))

# ---------------------------------------------------------------------------
# SMS / Africa's Talking Integration
# ---------------------------------------------------------------------------
DEMO_MODE: bool = os.getenv("DEMO_MODE", "true").lower() == "true"

# Provider selection: "africastalking" or "generic" (future)
SMS_PROVIDER: str = os.getenv("SMS_PROVIDER", "africastalking")

# Africa's Talking credentials
AFRICASTALKING_USERNAME: str = os.getenv("AFRICASTALKING_USERNAME", "sandbox")
AFRICASTALKING_API_KEY: str = os.getenv("AFRICASTALKING_API_KEY", "")
AFRICASTALKING_ENDPOINT: str = os.getenv(
    "AFRICASTALKING_ENDPOINT",
    "https://api.sandbox.africastalking.com/version1/messaging",
)
AFRICASTALKING_SENDER_ID: str = os.getenv("AFRICASTALKING_SENDER_ID", "ASPEN")

# ---------------------------------------------------------------------------
# Reverse Geocoding (Nominatim)
# ---------------------------------------------------------------------------
NOMINATIM_USER_AGENT: str = os.getenv("NOMINATIM_USER_AGENT", "ASPEN_Engine/1.0")
NOMINATIM_TIMEOUT: float = float(os.getenv("NOMINATIM_TIMEOUT", "2.0"))

# ---------------------------------------------------------------------------
# Engine Parameters
# ---------------------------------------------------------------------------
DISPATCH_BUFFER_HOURS: float = float(os.getenv("DISPATCH_BUFFER_HOURS", "1.5"))
AVG_SPEED_KMPH: float = float(os.getenv("AVG_SPEED_KMPH", "30.0"))
W_DISTANCE: float = float(os.getenv("W_DISTANCE", "0.6"))
W_FRESHNESS: float = float(os.getenv("W_FRESHNESS", "0.4"))
GLOBAL_D_MAX_KM: float = float(os.getenv("GLOBAL_D_MAX_KM", "200.0"))
GLOBAL_L_MAX_HOURS: float = float(os.getenv("GLOBAL_L_MAX_HOURS", "72.0"))

# ---------------------------------------------------------------------------
# System Infrastructure
# ---------------------------------------------------------------------------
STALE_MATCH_TIMEOUT_MINUTES: int = int(os.getenv("STALE_MATCH_TIMEOUT_MINUTES", "120"))

# Interval (in minutes) for expiry/watchdog tasks (spoilage, deadlines)
SCHEDULER_INTERVAL_MINUTES: int = int(os.getenv("SCHEDULER_INTERVAL_MINUTES", "1"))

# Interval (in minutes) for running the matching engine
ENGINE_INTERVAL_MINUTES: int = int(os.getenv("ENGINE_INTERVAL_MINUTES", "10"))

# ---------------------------------------------------------------------------
# Multi‑Phase Workflow Timings (tunable via .env)
# ---------------------------------------------------------------------------
ACCEPTANCE_WINDOW_MINUTES: int = int(os.getenv("ACCEPTANCE_WINDOW_MINUTES", "120"))
DISPATCH_WINDOW_MINUTES: int = int(os.getenv("DISPATCH_WINDOW_MINUTES", "120"))
DELIVERY_TRAVEL_MULTIPLIER: float = float(os.getenv("DELIVERY_TRAVEL_MULTIPLIER", "2.5"))
INSPECTION_BUFFER_HOURS: float = float(os.getenv("INSPECTION_BUFFER_HOURS", "6.0"))
MIN_DELIVERY_WINDOW_HOURS: float = float(os.getenv("MIN_DELIVERY_WINDOW_HOURS", "12.0"))

# ---------------------------------------------------------------------------
# Trust Score / Strike System (Phase 5)
# ---------------------------------------------------------------------------
SPOILAGE_CLAIM_THRESHOLD_RATIO: float = float(os.getenv("SPOILAGE_CLAIM_THRESHOLD_RATIO", "0.4"))
SPOILAGE_CLAIM_MIN_ORDERS: int = int(os.getenv("SPOILAGE_CLAIM_MIN_ORDERS", "5"))

# ---------------------------------------------------------------------------
# Helper to build an engine‑compatible configuration object
# ---------------------------------------------------------------------------
def get_engine_config():
    """Return an EngineConfig instance using the current web settings."""
    from app.engine.models import EngineConfig
    return EngineConfig(
        dispatch_buffer_h=DISPATCH_BUFFER_HOURS,
        avg_speed_kmph=AVG_SPEED_KMPH,
        w_distance=W_DISTANCE,
        w_freshness=W_FRESHNESS,
        global_max_distance_km=GLOBAL_D_MAX_KM,
        global_max_shelf_life_h=GLOBAL_L_MAX_HOURS,
    )