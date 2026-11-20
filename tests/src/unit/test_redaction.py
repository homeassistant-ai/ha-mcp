"""Secret redaction of app options and the known-value scrub (issue 2157).

App options and their schema are the Supervisor's, which a unit test cannot
run: they are written here in the shape the Supervisor serializes. The scrub
and the sentinel checks take any text or option value a caller sends.
Options-flow forms, the tools and the middleware are covered on real Core in
``test_redaction_flow_forms.py``, ``test_redaction_write_guards.py`` and
``test_redaction_middleware.py``.
"""

import json
from typing import Any

import pytest

from ha_mcp.redaction import (
    REDACTED_EMPTY,
    REDACTED_KNOWN,
    REDACTED_SET,
    collect_addon_secret_values,
    is_sentinel,
    known_secret_values,
    placeholder_option_keys,
    register_known_secret_values,
    scrub_obj,
    scrub_text,
    sentinel_for,
    sentinel_option_keys,
)

from ._redaction_registry import clean_registry as clean_registry
from ._redaction_registry import redacted_options

# The issue's motivating shape: a GitHub PAT marked format=password next to
# ordinary fields, plus a nested group with its own password leaf.
ADDON_SCHEMA = [
    {"name": "github_pat", "required": True, "type": "string", "format": "password"},
    {"name": "log_level", "type": "list", "options": ["info", "debug"]},
    {"name": "empty_password", "type": "string", "format": "password"},
    {
        "name": "ssh",
        "type": "schema",
        "schema": [
            {"name": "username", "type": "string"},
            {"name": "password", "type": "string", "format": "password"},
        ],
    },
    {
        "name": "accounts",
        "type": "schema",
        "multiple": True,
        "schema": [
            {"name": "user", "type": "string"},
            {"name": "token", "type": "string", "format": "password"},
        ],
    },
]

ADDON_OPTIONS = {
    "github_pat": "github_pat_LIVESECRETVALUE",
    "log_level": "info",
    "empty_password": "",
    "unlisted_key": "not in schema",
    "ssh": {"username": "root", "password": "hunter2secret"},
    "accounts": [
        {"user": "a", "token": "tok_aaaaaaaa"},
        {"user": "b", "token": ""},
    ],
}

PEM_MARKERS = [
    ("-----BEGIN OPENSSH PRIVATE KEY-----", "-----END OPENSSH PRIVATE KEY-----"),
    ("-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----"),
    ("-----BEGIN PGP PRIVATE KEY BLOCK-----", "-----END PGP PRIVATE KEY BLOCK-----"),
]
RSA_KEY_LINES = [
    "-----BEGIN RSA PRIVATE KEY-----",
    "secret",
    "-----END RSA PRIVATE KEY-----",
]
LIST_SCHEMA = [{"name": "deployment_key", "type": "string", "multiple": True}]


# --- app options ---------------------------------------------------------------------


def test_a_password_option_is_shown_as_set_not_its_value() -> None:
    out = redacted_options(ADDON_OPTIONS, ADDON_SCHEMA, "app")
    assert out["github_pat"] == REDACTED_SET


def test_an_empty_password_option_is_shown_as_empty() -> None:
    out = redacted_options(ADDON_OPTIONS, ADDON_SCHEMA, "app")
    assert out["empty_password"] == REDACTED_EMPTY


def test_ordinary_and_unlisted_options_are_shown_as_they_are() -> None:
    out = redacted_options(ADDON_OPTIONS, ADDON_SCHEMA, "app")
    assert out["log_level"] == "info"
    assert out["unlisted_key"] == "not in schema"


def test_a_password_inside_an_option_group_is_hidden() -> None:
    out = redacted_options(ADDON_OPTIONS, ADDON_SCHEMA, "app")
    assert out["ssh"] == {"username": "root", "password": REDACTED_SET}


def test_a_password_in_each_item_of_a_multiple_group_is_hidden() -> None:
    out = redacted_options(ADDON_OPTIONS, ADDON_SCHEMA, "app")
    assert out["accounts"] == [
        {"user": "a", "token": REDACTED_SET},
        {"user": "b", "token": REDACTED_EMPTY},
    ]


@pytest.mark.parametrize(("begin_marker", "end_marker"), PEM_MARKERS)
def test_a_private_key_the_schema_does_not_mark_is_still_hidden(
    begin_marker: str, end_marker: str
) -> None:
    options = {
        "deployment_key": f"{begin_marker}\nsecret\n{end_marker}",
        "description": "ordinary public configuration",
    }
    schema = [
        {"name": "deployment_key", "type": "string"},
        {"name": "description", "type": "string"},
    ]

    out = redacted_options(options, schema, "app")

    assert out["deployment_key"] == REDACTED_SET
    assert out["description"] == "ordinary public configuration"


def test_a_private_key_stored_as_a_list_of_lines_is_hidden_line_by_line() -> None:
    out = redacted_options({"deployment_key": RSA_KEY_LINES}, LIST_SCHEMA, "app")
    assert out["deployment_key"] == [REDACTED_SET] * 3


def test_redacting_leaves_the_apps_own_options_unchanged() -> None:
    before = json.dumps(ADDON_OPTIONS, sort_keys=True)
    redacted_options(ADDON_OPTIONS, ADDON_SCHEMA, "app")
    assert json.dumps(ADDON_OPTIONS, sort_keys=True) == before


def test_a_multiple_password_option_keeps_its_list_shape() -> None:
    schema = [
        {"name": "tokens", "type": "string", "format": "password", "multiple": True}
    ]
    options = {"tokens": ["tok_one_11", "", "tok_two_22"]}
    out = redacted_options(options, schema, "app")
    assert out["tokens"] == [REDACTED_SET, REDACTED_EMPTY, REDACTED_SET]


def test_redacting_a_redacted_read_again_changes_nothing() -> None:
    once = redacted_options(ADDON_OPTIONS, ADDON_SCHEMA, "app")
    twice = redacted_options(once, ADDON_SCHEMA, "app")
    assert twice == once


# --- what an app's options add to the scrub -------------------------------------------


def test_every_set_password_option_feeds_the_scrub_and_nothing_else() -> None:
    values = collect_addon_secret_values(ADDON_OPTIONS, ADDON_SCHEMA, "app")
    assert values == {
        "github_pat_LIVESECRETVALUE",
        "hunter2secret",
        "tok_aaaaaaaa",
    }


@pytest.mark.parametrize(("begin_marker", "end_marker"), PEM_MARKERS)
def test_an_unmarked_private_key_and_each_of_its_lines_feed_the_scrub(
    begin_marker: str, end_marker: str
) -> None:
    """The Supervisor's log reads split a multiline option before the scrub
    sees it, so each line is a known value too."""
    key = f"{begin_marker}\nsecret\n{end_marker}"
    values = collect_addon_secret_values(
        {"deployment_key": key},
        [{"name": "deployment_key", "type": "string"}],
        "app",
    )

    assert values == {key, begin_marker, "secret", end_marker}


def test_a_private_key_stored_as_lines_feeds_each_line_to_the_scrub() -> None:
    values = collect_addon_secret_values(
        {"deployment_key": RSA_KEY_LINES}, LIST_SCHEMA, "app"
    )

    assert values == set(RSA_KEY_LINES)


def test_a_multiline_key_in_a_list_item_feeds_the_item_and_its_lines() -> None:
    key = "\n".join(RSA_KEY_LINES)

    values = collect_addon_secret_values({"deployment_key": [key]}, LIST_SCHEMA, "app")

    assert values == {key, *RSA_KEY_LINES}


# --- sentinels and the values a write must refuse --------------------------------------


@pytest.mark.parametrize(
    ("value", "sentinel"),
    [
        ("value", REDACTED_SET),
        (0, REDACTED_SET),
        ("", REDACTED_EMPTY),
        (None, REDACTED_EMPTY),
        ([], REDACTED_EMPTY),
    ],
)
def test_a_redacted_value_still_says_whether_it_is_configured(
    value: Any, sentinel: str
) -> None:
    assert sentinel_for(value) == sentinel


@pytest.mark.parametrize(
    ("value", "sentinel"),
    [
        (REDACTED_SET, True),
        (REDACTED_EMPTY, True),
        (REDACTED_KNOWN, True),
        ("plain", False),
        (None, False),
    ],
)
def test_only_the_three_markers_are_sentinels(value: Any, sentinel: bool) -> None:
    assert is_sentinel(value) is sentinel


def test_a_sentinel_is_found_at_any_depth_of_submitted_options() -> None:
    options = {
        "a": REDACTED_SET,
        "b": "fine",
        "nested": {"c": REDACTED_EMPTY},
        "items": ["ok", REDACTED_KNOWN, {"d": REDACTED_SET}],
        "rows": [[REDACTED_SET], ["clean"]],
    }
    assert sentinel_option_keys(options) == [
        "a",
        "nested.c",
        "items[1]",
        "items[2].d",
        "rows[0][0]",
    ]


def test_clean_options_hold_no_sentinel() -> None:
    assert sentinel_option_keys({"a": 1, "b": {"c": "x"}, "d": [["y"]]}) == []


def test_a_value_with_a_sentinel_inside_it_is_refused_too() -> None:
    """The scrub can rewrite a secret inside a longer value; sending that
    value back would store the marker."""
    options = {
        "dsn": f"postgres://u:{REDACTED_KNOWN}@db/x",
        "ok": "clean-value",
        "items": [f"prefix {REDACTED_SET} suffix"],
    }
    assert sentinel_option_keys(options) == ["dsn", "items[0]"]


def test_config_entry_options_also_refuse_the_components_own_scrub() -> None:
    # The component writes "**redacted**" over an option equal to a
    # secrets.yaml value; app options never pass through it.
    options = {
        "a": REDACTED_SET,
        "b": "**redacted**",
        "nested": {"c": "**redacted**"},
        "items": ["ok", "**redacted**"],
    }
    assert placeholder_option_keys(options) == ["a", "b", "nested.c", "items[1]"]
    assert sentinel_option_keys(options) == ["a"]


# --- the known-value registry and scrub ------------------------------------------------


def test_a_value_too_short_to_scrub_safely_is_not_registered() -> None:
    register_known_secret_values(["abc", "12345", "long-enough-secret"])
    assert known_secret_values() == ("long-enough-secret",)


def test_a_value_that_is_not_text_is_not_registered() -> None:
    values: list[Any] = [None, 123456, "real-secret-value"]
    register_known_secret_values(values)
    assert known_secret_values() == ("real-secret-value",)


def test_registered_values_come_back_longest_first() -> None:
    register_known_secret_values(["shorter-1", "the-much-longer-secret"])
    assert known_secret_values() == ("the-much-longer-secret", "shorter-1")


def test_every_occurrence_of_a_secret_in_text_is_scrubbed() -> None:
    out = scrub_text("token=SECRETVALUE1 again SECRETVALUE1", ("SECRETVALUE1",))
    assert out == f"token={REDACTED_KNOWN} again {REDACTED_KNOWN}"


def test_a_secret_inside_a_longer_one_leaves_no_fragment_behind() -> None:
    register_known_secret_values(["SECRET-x", "SECRET-xLONGER"])
    out = scrub_text("a SECRET-xLONGER b SECRET-x c", known_secret_values())
    assert out == f"a {REDACTED_KNOWN} b {REDACTED_KNOWN} c"


def test_a_secret_is_scrubbed_from_values_lists_and_keys() -> None:
    obj = {
        "note": "uses SECRETVALUE1 here",
        "SECRETVALUE1": "as key",
        "nested": [{"v": "SECRETVALUE1"}, 42, None],
    }
    out = scrub_obj(obj, ("SECRETVALUE1",))
    assert out == {
        "note": f"uses {REDACTED_KNOWN} here",
        REDACTED_KNOWN: "as key",
        "nested": [{"v": REDACTED_KNOWN}, 42, None],
    }


def test_two_keys_scrubbed_to_the_same_marker_both_survive() -> None:
    obj = {"SECRETVALUE1": "a", "SECRETVALUE2": "b", "ok": "c"}
    out = scrub_obj(obj, ("SECRETVALUE1", "SECRETVALUE2"))
    assert out == {
        REDACTED_KNOWN: "a",
        f"{REDACTED_KNOWN}#2": "b",
        "ok": "c",
    }
