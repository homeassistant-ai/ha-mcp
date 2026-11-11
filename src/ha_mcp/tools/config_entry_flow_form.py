"""One flow form's submit, built the way Home Assistant's own dialog builds it.

The frontend opens a form filled with the values its schema carries and posts
the whole form back; a box the user empties is left out of the post. Core has
no patch semantics of its own: an options flow drops a stored optional value
that the post leaves out (issue #2254). So on a form that edits something the
caller's values go over the values the form shows, a value set to ``None`` is
an emptied box, and everything else reaches Core as written. Core's schema,
not this module, decides what is valid: a key the form does not declare is
sent along and refused by Core.
"""

import copy
from collections.abc import Iterator
from typing import Any

from ..redaction import carries_sentinel
from .helpers import reject_malformed_list_fields

MISSING = object()


def iter_schema_fields(data_schema: Any) -> Iterator[dict[str, Any]]:
    """Yield leaf field definitions from a flow schema, descending into sections."""
    if not isinstance(data_schema, list):
        return
    for field in data_schema:
        if not isinstance(field, dict):
            continue
        nested_schema = field.get("schema")
        if isinstance(nested_schema, list):
            yield from iter_schema_fields(nested_schema)
            continue
        yield field


def shown_value(field: dict[str, Any]) -> Any:
    """The value a form shows for ``field`` and its dialog posts back untouched:
    the suggested value (an edit form's stored one), or a constant's only
    value. ``MISSING`` when the form shows none.

    A bare ``default`` is not read: Core's schema applies it to a key left out.
    """
    description = field.get("description")
    if isinstance(description, dict) and description.get("suggested_value") is not None:
        return copy.deepcopy(description["suggested_value"])
    if field.get("type") == "constant" and field.get("value") is not None:
        return copy.deepcopy(field["value"])
    return MISSING


def _carries_sentinel(value: Any) -> bool:
    if isinstance(value, list):
        return any(carries_sentinel(item) for item in value)
    return carries_sentinel(value)


def form_submission(
    data_schema: Any, values: dict[str, Any], *, keep_shown: bool
) -> dict[str, Any]:
    """The post for one form: ``values`` over the values the form shows.

    ``keep_shown`` is for a form that edits an existing object: every field
    left out goes back as the form shows it. A form that sets something up
    gets only the required ones that way, since what it suggests for an
    optional field is a hint. A section's values are given under the
    section's name, as its schema nests them. A step without a schema gets
    ``values`` as written.
    """
    if not isinstance(data_schema, list):
        return {key: value for key, value in values.items() if value is not None}
    data: dict[str, Any] = {}
    declared: set[str] = set()
    for field in data_schema:
        name = field.get("name") if isinstance(field, dict) else None
        if not isinstance(name, str):
            continue
        declared.add(name)
        given = values.get(name, MISSING)
        nested = field.get("schema")
        if isinstance(nested, list) and (given is MISSING or isinstance(given, dict)):
            required = bool(field.get("required"))
            section = form_submission(
                nested,
                given if isinstance(given, dict) else {},
                keep_shown=keep_shown or required,
            )
            if section or required or given is not MISSING:
                data[name] = section
        elif given is MISSING:
            shown = (
                shown_value(field) if keep_shown or field.get("required") else MISSING
            )
            # A redacted read's placeholder is never written back over a secret.
            if shown is not MISSING and not _carries_sentinel(shown):
                data[name] = shown
        elif given is not None:
            # ActionSelector stores a mangled list unvalidated (#2548).
            if "action" in (field.get("selector") or {}):
                reject_malformed_list_fields({name: given}, (name,), {"field": name})
            data[name] = given
    # A key the form lacks goes to Core as written, null included: a null
    # empties a declared field only, and Core refuses the rest.
    data.update((key, value) for key, value in values.items() if key not in declared)
    return data
