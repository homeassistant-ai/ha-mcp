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

SUPPORTED_ALGORITHM = "pbkdf2_sha256"

# What the stored file amounts to, as far as every consumer is concerned.
# The three states are distinct on purpose: anything that is there and
# cannot be used -- unreadable, unparseable, or an object this build
# cannot decode -- is a configuration problem the user has to repair, not
# a PIN somebody typed wrongly, and the two must not be reported, or
# counted, as the same thing. Status, the guards that allow the toggle to
# be switched on, and verification all decide from this one answer, so the
# settings UI cannot advertise a PIN the listener would refuse.
PIN_ABSENT = "absent"
PIN_INVALID = "invalid"
PIN_SET = "set"


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
        "algorithm": SUPPORTED_ALGORITHM,
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


def _load_record(data_dir: Path) -> tuple[str, dict[str, Any] | None]:
    """The stored file as ``(state, record)``.

    Unreadable is not absent, and the difference has to survive the return
    rather than only reach the log: a file that exists and cannot be used
    is a repair job, while an absent one is a PIN nobody has set yet. Told
    apart, the settings UI can say which of the two it is instead of
    telling a user with a corrupt file to set the PIN they already set.

    ``PIN_SET`` here means only that an object was loaded; whether it is a
    record this build can verify against is ``_decode_record``'s half of
    the question, and ``pin_state`` puts the two together.
    """
    path = _pin_path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return PIN_ABSENT, None
    except (OSError, json.JSONDecodeError):
        logger.warning("approval PIN file %s is unreadable", path, exc_info=True)
        return PIN_INVALID, None
    if not isinstance(raw, dict):
        logger.warning("approval PIN file %s is not an object", path)
        return PIN_INVALID, None
    return PIN_SET, raw


def _decode_record(record: dict[str, Any]) -> tuple[bytes, bytes, int] | None:
    """The salt, expected digest and work factor of ``record``, or ``None``
    when it is not something this build can verify a PIN against.

    One half of what "a stored PIN" means; ``_load_record`` is the other,
    deciding whether there is an object here at all. An object that
    only looks like a record -- ``{}``, a record written by a future build
    naming another algorithm, a truncated salt -- fails here, and every
    consumer therefore treats it as a configuration problem instead of one
    reporting a PIN that another then refuses.
    """
    try:
        salt = base64.b64decode(record["salt"], validate=True)
        expected = base64.b64decode(record["hash"], validate=True)
        iterations = int(record["iterations"])
        algorithm = record["algorithm"]
    except (KeyError, TypeError, ValueError, binascii.Error):
        return None
    if algorithm != SUPPORTED_ALGORITHM or iterations < 1 or not salt or not expected:
        return None
    return salt, expected, iterations


def pin_state(data_dir: Path) -> str:
    """``PIN_ABSENT``, ``PIN_INVALID`` or ``PIN_SET`` for the stored file."""
    state, record = _load_record(data_dir)
    if record is None:
        return state
    if _decode_record(record) is None:
        logger.warning(
            "approval PIN file %s holds no usable record (expected a %r "
            "digest with a salt, a hash and a positive work factor); it "
            "cannot authorise anything until a new PIN is set",
            _pin_path(data_dir),
            SUPPORTED_ALGORITHM,
        )
        return PIN_INVALID
    return PIN_SET


def is_pin_set(data_dir: Path) -> bool:
    """Whether a PIN exists that a response event could actually match."""
    return pin_state(data_dir) == PIN_SET


def pin_status(data_dir: Path) -> dict[str, Any]:
    """What the settings UI may know about the PIN: that it exists, and when.

    A file that exists but cannot be verified against -- unreadable, not
    JSON, not an object, or an object this build cannot decode -- reports
    ``set: False`` with ``invalid: True``, so the tab offers to set a new
    PIN, and leaves the toggle it guards disabled, instead of either
    showing a PIN that works or telling the user to set one they have.
    """
    state, record = _load_record(data_dir)
    if record is None:
        return (
            {"set": False} if state == PIN_ABSENT else {"set": False, "invalid": True}
        )
    if _decode_record(record) is None:
        return {"set": False, "invalid": True}
    return {"set": True, "updated_at": record.get("updated_at")}


def verify_pin(data_dir: Path, candidate: Any) -> bool:
    """Whether ``candidate`` matches the stored PIN.

    False whenever anything is missing or malformed, including the file
    itself: this gates a decision, so the absence of a PIN must never read
    as a PIN that matched. Callers that count wrong PINs must check
    ``pin_state`` first -- a False from a record that cannot be decoded is
    a broken configuration, not a guess.
    """
    _state, record = _load_record(data_dir)
    if record is None or not isinstance(candidate, str):
        return False
    decoded = _decode_record(record)
    if decoded is None:
        logger.warning(
            "approval PIN record is malformed or names an unsupported "
            "algorithm; refusing to verify"
        )
        return False
    salt, expected, iterations = decoded
    return hmac.compare_digest(_derive(candidate, salt, iterations), expected)
