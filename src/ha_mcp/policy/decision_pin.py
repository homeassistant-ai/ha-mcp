"""The PIN that authorises an approval decision arriving over the event bus.

Deliberately NOT part of ``tool_policy.json``. That document is what
``ha_manage_security_policy`` reads and replaces, what the settings UI
round-trips on every save, and what a policy-version conflict echoes back
in an error body -- three ways for the material to end up somewhere it was
never meant to be. Keeping it in its own file means no read path that
serves the policy has to remember to redact it.

Stored as a PBKDF2-HMAC-SHA256 digest with a per-PIN salt. A PIN is short
and low-entropy by construction, so the work factor is the only thing
standing between a copy of this file and the PIN itself; it is paired with
the failed-attempt limiter in ``decisions.py``, which is what bounds
guessing against the live server.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PIN_FILENAME = "approval_pin.json"

# Short enough not to fight a numeric keypad on a phone notification,
# long enough that the limiter below has something to bound. The digits
# are the user's choice; nothing here requires them to be digits.
MIN_PIN_LENGTH = 4
MAX_PIN_LENGTH = 64

# PBKDF2 work factor. Verification runs once per response event -- at most
# MAX_FAILED_ATTEMPTS times per window -- so a quarter of a second on the
# slowest supported hardware buys the offline attacker four orders of
# magnitude over a bare digest and costs the user nothing they can feel.
HASH_ITERATIONS = 200_000
_SALT_BYTES = 16


def _pin_path(data_dir: Path) -> Path:
    return data_dir / PIN_FILENAME


def _derive(pin: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)


def validate_pin(pin: Any) -> str:
    """Return the PIN if it is usable, else raise ``ValueError``.

    Length is the only rule. A composition rule (digits, mixed case) would
    shrink the search space it pretends to grow, and the honest statement
    about this feature -- an agent with enough access can obtain the PIN --
    is not one a stricter alphabet changes.
    """
    if not isinstance(pin, str):
        raise ValueError("pin must be a string")
    if len(pin) < MIN_PIN_LENGTH:
        raise ValueError(f"pin must be at least {MIN_PIN_LENGTH} characters")
    if len(pin) > MAX_PIN_LENGTH:
        raise ValueError(f"pin must be at most {MAX_PIN_LENGTH} characters")
    return pin


def set_pin(data_dir: Path, pin: str) -> None:
    """Persist ``pin`` as a salted digest, replacing any previous one."""
    validate_pin(pin)
    salt = secrets.token_bytes(_SALT_BYTES)
    record = {
        "algorithm": "pbkdf2_sha256",
        "iterations": HASH_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(_derive(pin, salt, HASH_ITERATIONS)).decode("ascii"),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    path = _pin_path(data_dir)
    # mkstemp creates at 0600, and os.replace carries that mode onto the
    # final file -- the digest never exists at the default umask, not even
    # for the width of the write.
    fd, tmp_path = tempfile.mkstemp(prefix=f".{PIN_FILENAME}.", dir=data_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def clear_pin(data_dir: Path) -> bool:
    """Delete the stored PIN. Returns False when there was none."""
    path = _pin_path(data_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _load_record(data_dir: Path) -> dict[str, Any] | None:
    path = _pin_path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        # Unreadable is not "absent": say so, and let every caller treat it
        # as a PIN that cannot authorise anything rather than as an open door.
        logger.warning("approval PIN file %s is unreadable", path, exc_info=True)
        return None
    if not isinstance(raw, dict):
        logger.warning("approval PIN file %s is not an object", path)
        return None
    return raw


def is_pin_set(data_dir: Path) -> bool:
    """Whether a usable PIN exists."""
    return _load_record(data_dir) is not None


def pin_status(data_dir: Path) -> dict[str, Any]:
    """What the settings UI may know about the PIN: that it exists, and when."""
    record = _load_record(data_dir)
    if record is None:
        return {"set": False}
    return {"set": True, "updated_at": record.get("updated_at")}


def verify_pin(data_dir: Path, candidate: Any) -> bool:
    """Whether ``candidate`` matches the stored PIN.

    False whenever anything is missing or malformed, including the file
    itself: this gates a decision, so the absence of a PIN must never read
    as a PIN that matched.
    """
    record = _load_record(data_dir)
    if record is None or not isinstance(candidate, str):
        return False
    try:
        salt = base64.b64decode(record["salt"], validate=True)
        expected = base64.b64decode(record["hash"], validate=True)
        iterations = int(record["iterations"])
        algorithm = record["algorithm"]
    except (KeyError, TypeError, ValueError, binascii.Error):
        logger.warning("approval PIN record is malformed; refusing to verify")
        return False
    if algorithm != "pbkdf2_sha256" or iterations < 1:
        logger.warning(
            "approval PIN record names an unsupported algorithm (%r) or "
            "work factor (%r); refusing to verify",
            algorithm,
            iterations,
        )
        return False
    return hmac.compare_digest(_derive(candidate, salt, iterations), expected)
