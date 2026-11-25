"""Helpers for automation configuration writes."""

from typing import Any


def strip_redundant_identifier_echo(
    result: dict[str, Any],
    *,
    extra_excludes: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Strip the REST client's ``identifier`` echo from a write's result.

    ``automation_id`` (the resolved entity_id, else the input entity_id, else
    the config id) already names the automation. ``extra_excludes`` drops
    other keys the caller sets itself. Never exclude ``unique_id``: callers
    track the config id for cleanup (E2E test_duplicate_automation_prevention).
    """
    excluded = {"identifier", *extra_excludes}
    return {k: v for k, v in result.items() if k not in excluded}


