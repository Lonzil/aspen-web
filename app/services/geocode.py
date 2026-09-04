"""
Reverse geocoding service (Nominatim / OpenStreetMap)
and offline static location lookup (Ghana places).
"""

import json
import logging
from pathlib import Path
from typing import Optional

import httpx

from app.config import NOMINATIM_TIMEOUT, NOMINATIM_USER_AGENT

logger = logging.getLogger("aspen.geocode")


async def reverse_geocode(lat: float, lon: float) -> Optional[str]:
    """
    Attempt to get a human-readable place name for the given coordinates.
    Returns the place name (str) or None if the request fails or times out.
    """
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {
        "lat": lat,
        "lon": lon,
        "format": "json",
        "addressdetails": 1,
        "zoom": 14,
    }
    headers = {"User-Agent": NOMINATIM_USER_AGENT}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                params=params,
                headers=headers,
                timeout=NOMINATIM_TIMEOUT,
            )
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            logger.warning("Nominatim error for %.4f,%.4f: %s", lat, lon, data["error"])
            return None

        # Extract a useful label: prefer city/town/village, fallback to county/state
        address = data.get("address", {})
        label = (
            address.get("city")
            or address.get("town")
            or address.get("village")
            or address.get("county")
            or address.get("state")
            or data.get("display_name", None)
        )
        return label

    except httpx.TimeoutException:
        logger.warning("Nominatim timeout for %.4f,%.4f", lat, lon)
        return None
    except Exception:
        logger.exception("Nominatim request failed for %.4f,%.4f", lat, lon)
        return None


# ---------------------------------------------------------------------------
# Static location lookup (offline) – used for Region → District → Town
# ---------------------------------------------------------------------------
_STATIC_DATA = None  # in‑memory cache


def _load_ghana_places() -> dict:
    """Load the offline ghana_places.json file, caching it in memory."""
    global _STATIC_DATA
    if _STATIC_DATA is not None:
        return _STATIC_DATA

    # Path is relative to this file:  app/services/geocode.py → data/ghana_places.json
    data_path = Path(__file__).resolve().parent.parent.parent / "data" / "ghana_places.json"
    with open(data_path, "r", encoding="utf-8") as f:
        _STATIC_DATA = json.load(f)
    logger.info("Loaded static Ghana places from %s", data_path)
    return _STATIC_DATA


def get_coordinates_from_static(
    region: str,
    district: str,
    town: str,
) -> Optional[tuple[float, float]]:
    """
    Return (lat, lon) for a given Region → District → Town using the static
    Ghana places data.  Returns None if the combination is not found.

    The lookup is **case‑insensitive** and ignores leading/trailing whitespace.
    """
    data = _load_ghana_places()

    # 1. Match region (case‑insensitive)
    region_key = region.strip().lower()
    region_data = None
    for key in data:
        if key.strip().lower() == region_key:
            region_data = data[key]
            break
    if not region_data:
        logger.warning("Region '%s' not found in static data", region)
        return None

    # 2. Match district within the region
    district_key = district.strip().lower()
    district_towns = None
    for key in region_data:
        if key.strip().lower() == district_key:
            district_towns = region_data[key]
            break
    if not district_towns:
        logger.warning("District '%s' not found in region '%s'", district, region)
        return None

    # 3. Match town within the district
    town_key = town.strip().lower()
    for entry in district_towns:
        if entry["name"].strip().lower() == town_key:
            return float(entry["lat"]), float(entry["lon"])

    logger.warning("Town '%s' not found in district '%s', region '%s'", town, district, region)
    return None