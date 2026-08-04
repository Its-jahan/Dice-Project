"""The front door.

Why the app grew a login at all
-------------------------------
The deployment was protected by nginx Basic Auth, which works but hands the
user the browser's native credential box: no branding, no way to sign out, no
lockout after a thousand guesses, and a password the browser re-sends on every
request forever. This replaces it with a session the server can see, count and
revoke.

What it protects
----------------
Not much data — but the server holds a Dune key, a model key and an Alchemy
token, and it can spend all three. An open box is not a privacy problem here,
it is a billing one, which is why the throttle below is per-address and
persistent rather than a nicety.

Choices worth knowing
---------------------
**scrypt, from the standard library.** No new dependency, and deliberately
slow: the whole point of a password hash is that guessing is expensive.

**The session token is stored hashed.** A leaked database then yields no usable
sessions — the same reason the password is not stored either.

**Failed attempts live in SQLite, not memory.** An in-memory counter resets on
restart, and "restart the process" is not a lock anyone should be able to pick.

**One password, no username.** This is a single-user deployment. A username
field that accepts anything is theatre, and theatre in an auth screen teaches
people the wrong thing about what is checked.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db

#: scrypt parameters. n=2**15 costs roughly 100ms per attempt on a small VPS,
#: which is unnoticeable once and ruinous a million times over.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LEN = 64

#: scrypt needs 128 * n * r bytes — about 34 MB at these settings, which is
#: just over OpenSSL's 32 MB default and fails with "memory limit exceeded".
#: Raise the ceiling rather than weaken the hash: the memory hardness is the
#: property being bought.
SCRYPT_MAXMEM = 128 * SCRYPT_N * SCRYPT_R * 2

SESSION_COOKIE = "dice_session"

#: A session without "keep me signed in". Long enough to work through an
#: afternoon, short enough that a borrowed laptop is not a standing invitation.
SESSION_HOURS = 12
REMEMBERED_DAYS = 30

#: The throttle. Six wrong answers buys a fifteen-minute pause, per address.
#: Slow enough to make guessing pointless, generous enough to survive a
#: genuinely forgotten password without locking the owner out for a day.
MAX_ATTEMPTS = 6
LOCKOUT_MINUTES = 15
ATTEMPT_WINDOW_MINUTES = 15


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


# --------------------------------------------------------------- the password


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        dklen=KEY_LEN, maxmem=SCRYPT_MAXMEM,
    )


def set_password(password: str) -> None:
    """Store a new password. The old one stops working immediately."""
    if not password or len(password) < 8:
        raise ValueError("The password must be at least 8 characters.")
    salt = secrets.token_bytes(16)
    db.set_setting("auth_salt", salt.hex())
    db.set_setting("auth_hash", _hash(password, salt).hex())
    # A password change is also the way to boot out whoever else was signed in.
    revoke_all_sessions()


def password_is_set() -> bool:
    return bool(db.get_setting("auth_hash") and db.get_setting("auth_salt"))


def check_password(password: str) -> bool:
    """Constant-time comparison, so timing cannot leak a prefix."""
    stored, salt_hex = db.get_setting("auth_hash"), db.get_setting("auth_salt")
    if not stored or not salt_hex:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(stored)
    except ValueError:  # pragma: no cover - corrupt setting
        return False
    return hmac.compare_digest(_hash(password, salt), expected)


# --------------------------------------------------------------- the throttle


def lockout_remaining(address: str) -> int:
    """Seconds until this address may try again. Zero when it may try now."""
    since = _iso(_utcnow() - timedelta(minutes=ATTEMPT_WINDOW_MINUTES))
    failures = db.count_login_failures(address, since)
    if failures < MAX_ATTEMPTS:
        return 0
    last = db.last_login_failure(address)
    if last is None:
        return 0
    try:
        when = datetime.fromisoformat(last)
    except ValueError:  # pragma: no cover - corrupt row
        return 0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    remaining = (when + timedelta(minutes=LOCKOUT_MINUTES)) - _utcnow()
    return max(0, int(remaining.total_seconds()))


def count_recent_failures(address: str) -> int:
    """Failures inside the sliding window, for telling someone how many are left."""
    since = _iso(_utcnow() - timedelta(minutes=ATTEMPT_WINDOW_MINUTES))
    return db.count_login_failures(address, since)


def record_failure(address: str) -> None:
    db.record_login_failure(address, _iso(_utcnow()))


def clear_failures(address: str) -> None:
    """A correct password wipes the slate, so one bad day cannot accumulate."""
    db.clear_login_failures(address)


# --------------------------------------------------------------- the session


def _token_hash(token: str) -> str:
    # SHA-256 rather than scrypt: the token is 256 bits of randomness, so there
    # is nothing to brute force and no reason to pay for a slow hash.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def start_session(*, remember: bool = False) -> tuple[str, datetime]:
    """Mint a session and return the raw token — the only time it exists."""
    token = secrets.token_urlsafe(32)
    expires = _utcnow() + (
        timedelta(days=REMEMBERED_DAYS) if remember else timedelta(hours=SESSION_HOURS)
    )
    db.create_session(_token_hash(token), _iso(_utcnow()), _iso(expires))
    return token, expires


def session_is_valid(token: str | None) -> bool:
    if not token:
        return False
    return db.session_is_live(_token_hash(token), _iso(_utcnow()))


def end_session(token: str | None) -> None:
    if token:
        db.delete_session(_token_hash(token))


def revoke_all_sessions() -> None:
    db.delete_all_sessions()


def cookie_kwargs(expires: datetime, *, secure: bool) -> dict[str, Any]:
    """How the cookie is set. HttpOnly always — JavaScript has no business
    reading it, and that is what turns an XSS into a session theft.
    """
    return {
        "key": SESSION_COOKIE,
        "httponly": True,
        # Lax rather than Strict: Strict would drop the cookie when arriving
        # from an external link, which looks exactly like being logged out.
        "samesite": "lax",
        "secure": secure,
        "max_age": max(1, int((expires - _utcnow()).total_seconds())),
        "path": "/",
    }


__all__ = [
    "LOCKOUT_MINUTES",
    "MAX_ATTEMPTS",
    "SESSION_COOKIE",
    "check_password",
    "clear_failures",
    "count_recent_failures",
    "cookie_kwargs",
    "end_session",
    "lockout_remaining",
    "password_is_set",
    "record_failure",
    "revoke_all_sessions",
    "session_is_valid",
    "set_password",
    "start_session",
]
