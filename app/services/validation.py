"""
Centralised Validation Helpers
===============================
Pure, reusable validation functions for ASPEN.

These functions raise `ValueError` on invalid input and return
normalised values where appropriate. They have no external
dependencies and are safe to use in Pydantic validators,
FastAPI routes, and server‑rendered form handling.
"""

import re

# ---------------------------------------------------------------------------
# Phone Validation
# ---------------------------------------------------------------------------
def validate_ghana_phone(phone: str) -> str:
    """
    Validate and normalise a Ghana phone number.

    Accepts:
      - 0XXXXXXXXX  (10 digits, starts with 0)
      - 0XX XXX XXXX (with spaces)
      - +233XXXXXXXXX (12 digits, starts with +233)
      - 233XXXXXXXXX  (without '+')

    Returns canonical form: 0XXXXXXXXX (10 digits, no spaces)
    Raises ValueError if invalid.
    """
    if not isinstance(phone, str):
        raise ValueError("Phone number must be a string.")

    cleaned = phone.replace(' ', '').replace('-', '')
    if not cleaned:
        raise ValueError("Phone number is required.")

    # Remove leading + if present
    if cleaned.startswith('+'):
        cleaned = cleaned[1:]

    # If starts with 233, convert to 0
    if cleaned.startswith('233') and len(cleaned) == 12:
        cleaned = '0' + cleaned[3:]
    elif cleaned.startswith('0') and len(cleaned) == 10:
        pass  # already canonical
    else:
        raise ValueError("Invalid Ghana phone number. Use 0XX XXX XXXX or +233XXXXXXXXX.")

    # Ensure all digits
    if not cleaned.isdigit():
        raise ValueError("Phone number must contain only digits.")
    return cleaned


# ---------------------------------------------------------------------------
# GPS Validation
# ---------------------------------------------------------------------------
def validate_gps(lat: float, lon: float):
    """
    Validate GPS coordinates.
    Returns (lat, lon) or raises ValueError if out of range.
    """
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        raise ValueError("Latitude and longitude must be numbers.")
    if not (-90.0 <= lat <= 90.0):
        raise ValueError("Latitude must be between -90 and 90.")
    if not (-180.0 <= lon <= 180.0):
        raise ValueError("Longitude must be between -180 and 180.")
    return lat, lon


# ---------------------------------------------------------------------------
# Crop Type Validation
# ---------------------------------------------------------------------------
def validate_crop_type(crop: str) -> str:
    """
    Trim, lowercase, and validate crop type.
    Raises ValueError if empty or too long.
    """
    if not isinstance(crop, str):
        raise ValueError("Crop type must be a string.")
    crop = crop.strip().lower()
    if not crop:
        raise ValueError("Crop type cannot be empty.")
    if len(crop) > 50:
        raise ValueError("Crop type must be at most 50 characters.")
    return crop


# ---------------------------------------------------------------------------
# File Validation
# ---------------------------------------------------------------------------
def validate_file_size(file, max_bytes: int = 5 * 1024 * 1024):
    """
    Check uploaded file size. Accepts a file-like object with .file or .size.
    Raises ValueError if file exceeds max_bytes.
    """
    size = None
    if hasattr(file, "size"):
        size = file.size
    elif hasattr(file, "file") and hasattr(file.file, "seek"):
        current = file.file.tell()
        file.file.seek(0, 2)
        size = file.file.tell()
        file.file.seek(current)
    if size is None:
        raise ValueError("Unable to determine file size.")
    if size > max_bytes:
        raise ValueError(f"File size exceeds maximum allowed ({max_bytes // (1024*1024)} MB).")


def validate_file_extension(filename: str, allowed_extensions=None):
    """
    Check file extension. Accepts filename and optional set of allowed extensions.
    Default allowed: {'.jpg', '.jpeg', '.png'}
    Raises ValueError if extension not allowed.
    """
    if not isinstance(filename, str):
        raise ValueError("Filename must be a string.")
    if allowed_extensions is None:
        allowed_extensions = {'.jpg', '.jpeg', '.png'}
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    ext = '.' + ext if ext else ''
    if ext not in allowed_extensions:
        raise ValueError(f"File extension '{ext}' is not allowed. Allowed: {', '.join(allowed_extensions)}")


# ---------------------------------------------------------------------------
# CSV Validation
# ---------------------------------------------------------------------------
def validate_csv_size(contents, max_bytes: int = 5 * 1024 * 1024):
    """
    Check CSV content size (bytes).
    Raises ValueError if exceeds max_bytes.
    """
    if isinstance(contents, str):
        size = len(contents.encode('utf-8'))
    elif isinstance(contents, bytes):
        size = len(contents)
    else:
        raise ValueError("CSV contents must be str or bytes.")
    if size > max_bytes:
        raise ValueError(f"CSV file size exceeds maximum allowed ({max_bytes // (1024*1024)} MB).")


def validate_csv_row_count(rows, max_rows: int = 1000):
    """
    Check number of CSV rows.
    Raises ValueError if exceeds max_rows.
    """
    count = len(rows) if rows is not None else 0
    if count > max_rows:
        raise ValueError(f"CSV has too many rows (max {max_rows}).")


# ---------------------------------------------------------------------------
# Password Policy
# ---------------------------------------------------------------------------
def validate_password_policy(password: str) -> str:
    """
    Return error message if password doesn't meet policy, else empty string.
    """
    if len(password) < 8:
        return "Password must be at least 8 characters long."
    if len(password) > 72:
        return "Password must be at most 72 characters long."
    if not re.search(r'[a-z]', password):
        return "Password must include at least one lowercase letter."
    if not re.search(r'[A-Z]', password):
        return "Password must include at least one uppercase letter."
    if not re.search(r'\d', password):
        return "Password must include at least one digit."
    if not re.search(r'[^a-zA-Z\d]', password):
        return "Password must include at least one special character."
    return ""