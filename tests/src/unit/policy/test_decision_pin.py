"""Test the stored approval PIN.

The security-relevant properties are that the PIN itself is never written
down, that anything other than the right PIN verifies as false -- including
every way the file can be missing or broken -- and that the file cannot be
read by other users on the host.
"""

from __future__ import annotations

import json
import stat

import pytest

from ha_mcp.policy.decision_pin import (
    MAX_HASH_ITERATIONS,
    MAX_PIN_LENGTH,
    MIN_PIN_LENGTH,
    PIN_ABSENT,
    PIN_FILENAME,
    PIN_INVALID,
    PIN_SET,
    clear_pin,
    is_pin_set,
    pin_state,
    pin_status,
    set_pin,
    validate_pin,
    verify_pin,
)


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch: pytest.MonkeyPatch):
    """Keep the tests quick without changing what they test.

    The work factor is a cost, not a behaviour: every assertion here holds
    at any iteration count, and the stored record carries its own so a
    lowered one still round-trips.
    """
    monkeypatch.setattr("ha_mcp.policy.decision_pin.HASH_ITERATIONS", 1000)


def test_a_set_pin_verifies(tmp_path):
    set_pin(tmp_path, "2468")

    assert is_pin_set(tmp_path) is True
    assert verify_pin(tmp_path, "2468") is True


def test_a_wrong_pin_does_not_verify(tmp_path):
    set_pin(tmp_path, "2468")

    assert verify_pin(tmp_path, "2469") is False
    assert verify_pin(tmp_path, "") is False
    assert verify_pin(tmp_path, None) is False
    assert verify_pin(tmp_path, 2468) is False


def test_the_pin_is_not_stored_in_the_clear(tmp_path):
    set_pin(tmp_path, "2468")

    record = json.loads((tmp_path / PIN_FILENAME).read_text())
    assert "2468" not in json.dumps(record)
    assert record["algorithm"] == "pbkdf2_sha256"
    assert record["salt"] and record["hash"]


def test_two_stores_of_the_same_pin_differ(tmp_path, tmp_path_factory):
    """A per-PIN salt, so one cracked file says nothing about the next."""
    other = tmp_path_factory.mktemp("other")
    set_pin(tmp_path, "2468")
    set_pin(other, "2468")

    first = json.loads((tmp_path / PIN_FILENAME).read_text())
    second = json.loads((other / PIN_FILENAME).read_text())
    assert first["salt"] != second["salt"]
    assert first["hash"] != second["hash"]


def test_the_file_is_not_readable_by_others(tmp_path):
    set_pin(tmp_path, "2468")

    mode = (tmp_path / PIN_FILENAME).stat().st_mode
    assert not mode & stat.S_IRGRP
    assert not mode & stat.S_IROTH


def test_setting_again_replaces_the_previous_pin(tmp_path):
    set_pin(tmp_path, "2468")
    set_pin(tmp_path, "1357")

    assert verify_pin(tmp_path, "1357") is True
    assert verify_pin(tmp_path, "2468") is False


def test_absent_pin_verifies_nothing(tmp_path):
    assert is_pin_set(tmp_path) is False
    assert verify_pin(tmp_path, "2468") is False
    assert verify_pin(tmp_path, "") is False
    assert pin_status(tmp_path) == {"set": False}


def test_clear_removes_the_pin(tmp_path):
    set_pin(tmp_path, "2468")

    assert clear_pin(tmp_path) is True
    assert is_pin_set(tmp_path) is False
    assert verify_pin(tmp_path, "2468") is False
    assert clear_pin(tmp_path) is False


def test_status_reports_existence_and_age_only(tmp_path):
    set_pin(tmp_path, "2468")

    status = pin_status(tmp_path)
    assert status["set"] is True
    assert status["updated_at"]
    assert "hash" not in status and "salt" not in status


@pytest.mark.parametrize(
    "record",
    [
        "not-an-object",
        {"algorithm": "pbkdf2_sha256", "iterations": 1000, "salt": "AAAA"},
        {"algorithm": "md5", "iterations": 1000, "salt": "AAAA", "hash": "AAAA"},
        {
            "algorithm": "pbkdf2_sha256",
            "iterations": 0,
            "salt": "AAAA",
            "hash": "AAAA",
        },
        {
            "algorithm": "pbkdf2_sha256",
            "iterations": "many",
            "salt": "AAAA",
            "hash": "AAAA",
        },
        {"algorithm": "pbkdf2_sha256", "iterations": 1000, "salt": "!", "hash": "!"},
    ],
    ids=[
        "not-object",
        "no-hash",
        "wrong-algorithm",
        "no-work",
        "bad-iterations",
        "bad-base64",
    ],
)
def test_a_broken_record_verifies_nothing(tmp_path, record):
    """A file that cannot be understood must not read as a PIN that matched."""
    (tmp_path / PIN_FILENAME).write_text(json.dumps(record))

    assert verify_pin(tmp_path, "2468") is False


@pytest.mark.parametrize(
    "record",
    [
        {},
        {"updated_at": "2026-01-01T00:00:00+00:00"},
        {"algorithm": "pbkdf2_sha256", "iterations": 1000, "salt": "AAAA"},
        {"algorithm": "md5", "iterations": 1000, "salt": "AAAA", "hash": "AAAA"},
        {"algorithm": "pbkdf2_sha256", "iterations": 1000, "salt": "", "hash": ""},
    ],
    ids=["empty-object", "timestamp-only", "no-hash", "wrong-algorithm", "empty-salt"],
)
def test_a_record_nobody_can_match_is_invalid_not_set(tmp_path, record):
    """One answer for every consumer, so none can advertise what another refuses.

    Status drives the settings UI, ``is_pin_set`` drives the guards that
    allow the toggle to be switched on, and verification drives the
    listener. An object that merely looks like a record used to satisfy the
    first two and fail the third: the tab reported a PIN, the toggle went
    on, and every response event was then refused -- while spending the
    wrong-PIN budget on a configuration problem.
    """
    (tmp_path / PIN_FILENAME).write_text(json.dumps(record))

    assert pin_state(tmp_path) == PIN_INVALID
    assert is_pin_set(tmp_path) is False
    assert pin_status(tmp_path) == {"set": False, "invalid": True}
    assert verify_pin(tmp_path, "2468") is False


def test_a_work_factor_above_the_ceiling_is_invalid_not_set(tmp_path):
    """The factor comes out of the file, so the file can name an absurd one.

    Verification spends it in a worker thread, so a record restored from a
    damaged or doctored file would otherwise tie one up for as long as the
    number says while still reporting a usable PIN. The ceiling is the same
    kind of answer as every other unreadable record: a repair job. Both
    arms are asserted because only the boundary discriminates -- a bound
    that rejected the legitimate factor too would satisfy the first
    assertion just as well.
    """

    def record(iterations: int) -> str:
        return json.dumps(
            {
                "algorithm": "pbkdf2_sha256",
                "iterations": iterations,
                "salt": "AAAA",
                "hash": "AAAA",
            }
        )

    path = tmp_path / PIN_FILENAME

    path.write_text(record(MAX_HASH_ITERATIONS))
    assert pin_state(tmp_path) == PIN_SET

    path.write_text(record(MAX_HASH_ITERATIONS + 1))
    assert pin_state(tmp_path) == PIN_INVALID
    assert is_pin_set(tmp_path) is False
    assert pin_status(tmp_path) == {"set": False, "invalid": True}
    assert verify_pin(tmp_path, "2468") is False


def test_the_three_states_are_distinguishable(tmp_path):
    """Absent and invalid are different problems with different repairs."""
    assert pin_state(tmp_path) == PIN_ABSENT
    assert pin_status(tmp_path) == {"set": False}

    (tmp_path / PIN_FILENAME).write_text("{}")
    assert pin_state(tmp_path) == PIN_INVALID

    set_pin(tmp_path, "2468")
    assert pin_state(tmp_path) == PIN_SET
    assert pin_status(tmp_path)["set"] is True


@pytest.mark.parametrize(
    "content",
    ["{ not json", "[]", '"a string"', ""],
    ids=["unparseable", "json-array", "json-string", "empty-file"],
)
def test_a_file_that_cannot_be_loaded_is_invalid_not_absent(tmp_path, content):
    """A file that is there and unusable is a repair job, not a missing PIN.

    Reported as absent, every consumer tells the user to set a PIN they
    have already set: the tab says "No PIN set", the listener refuses
    events with "no PIN is configured", and a policy save that enables the
    channel is rejected for a missing PIN. The file is right there.
    """
    (tmp_path / PIN_FILENAME).write_text(content)

    assert pin_state(tmp_path) == PIN_INVALID
    assert pin_status(tmp_path) == {"set": False, "invalid": True}
    assert is_pin_set(tmp_path) is False
    assert verify_pin(tmp_path, "2468") is False


def test_no_file_at_all_is_absent(tmp_path):
    """The control for the test above: nothing there is not "invalid"."""
    assert pin_state(tmp_path) == PIN_ABSENT
    assert pin_status(tmp_path) == {"set": False}
    assert is_pin_set(tmp_path) is False


@pytest.mark.parametrize(
    "candidate",
    ["", "1", "x" * (MIN_PIN_LENGTH - 1), "x" * (MAX_PIN_LENGTH + 1), 1234, None],
)
def test_validate_rejects_unusable_pins(candidate):
    with pytest.raises(ValueError):
        validate_pin(candidate)


def test_validate_accepts_the_boundaries():
    assert validate_pin("x" * MIN_PIN_LENGTH)
    assert validate_pin("x" * MAX_PIN_LENGTH)


def test_set_refuses_a_too_short_pin(tmp_path):
    with pytest.raises(ValueError):
        set_pin(tmp_path, "12")

    assert is_pin_set(tmp_path) is False
