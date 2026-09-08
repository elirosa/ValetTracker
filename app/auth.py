"""PIN login backed by an HMAC-signed session cookie.

No user accounts on purpose: a valet stand is a shared podium and a shared
tablet. Two PINs give you the only distinction that matters in practice —
whoever can run the stand, and whoever can delete records.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
STAFF_PIN = os.environ.get("STAFF_PIN", "").strip()
ADMIN_PIN = os.environ.get("ADMIN_PIN", "").strip()
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() in ("1", "true", "yes")
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "12"))
COOKIE_NAME = "vt_session"

_ephemeral_secret = False
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    _ephemeral_secret = True

_fails = {}
_fail_lock = threading.Lock()
MAX_FAILS = 10
LOCKOUT_SECONDS = 300


def secret_is_ephemeral() -> bool:
    return _ephemeral_secret


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: bytes) -> str:
    return _b64e(hmac.new(SECRET_KEY.encode(), payload, hashlib.sha256).digest())


def make_token(role: str) -> str:
    payload = json.dumps({
        "role": role,
        "exp": int(time.time()) + SESSION_HOURS * 3600,
        "jti": secrets.token_hex(6),
    }, separators=(",", ":")).encode()
    return f"{_b64e(payload)}.{_sign(payload)}"


def read_token(token: str):
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    try:
        payload = _b64d(body)
    except Exception:
        return None
    if not hmac.compare_digest(_sign(payload), sig):
        return None
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if data.get("exp", 0) < time.time():
        return None
    return data


def locked_out(ip: str) -> bool:
    with _fail_lock:
        count, until = _fails.get(ip, (0, 0))
        if until and until > time.time():
            return True
        if until and until <= time.time():
            _fails.pop(ip, None)
        return False


def record_failure(ip: str):
    with _fail_lock:
        count, _ = _fails.get(ip, (0, 0))
        count += 1
        until = time.time() + LOCKOUT_SECONDS if count >= MAX_FAILS else 0
        _fails[ip] = (count, until)


def clear_failures(ip: str):
    with _fail_lock:
        _fails.pop(ip, None)


def role_for_pin(pin: str):
    """Constant-time compare both PINs so timing can't distinguish them."""
    pin = (pin or "").strip()
    if not pin:
        return None
    if ADMIN_PIN and hmac.compare_digest(pin, ADMIN_PIN):
        return "admin"
    if STAFF_PIN and hmac.compare_digest(pin, STAFF_PIN):
        return "valet"
    return None


def cookie_header(token: str) -> str:
    parts = [
        f"{COOKIE_NAME}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={SESSION_HOURS * 3600}",
    ]
    if COOKIE_SECURE:
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie_header() -> str:
    parts = [f"{COOKIE_NAME}=", "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0"]
    if COOKIE_SECURE:
        parts.append("Secure")
    return "; ".join(parts)
