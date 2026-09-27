"""List-shape checks for Lovelace dashboard configs and JSON Patch edits (issue #2548).

Covers only the positions the frontend types as arrays: ``views``, view
``cards`` / ``sections`` / ``badges``, section ``cards``, and the ``cards`` of
core stack cards (also inside core wrapper cards).
"""

import re
from typing import Any, Literal

from .helpers import reject_malformed_list_fields
from .util_helpers import loads_if_json_container_str

# Core stack cards; custom cards may give ``cards`` any shape, so they are skipped.
_STACK_CARD_TYPES = frozenset({"vertical-stack", "horizontal-stack", "grid"})
# Core cards that wrap one card under ``card``.
_CARD_WRAPPER_TYPES = frozenset({"conditional", "entity-filter"})


def _items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _collect_card_lists(card: Any, path: str, fields: dict[str, Any]) -> None:
    if not isinstance(card, dict):
        return
    if card.get("type") in _CARD_WRAPPER_TYPES:
        _collect_card_lists(card.get("card"), f"{path}.card", fields)
    elif card.get("type") in _STACK_CARD_TYPES:
        fields[f"{path}.cards"] = card.get("cards")
        for i, child in enumerate(_items(card.get("cards"))):
            _collect_card_lists(child, f"{path}.cards[{i}]", fields)


def _collect_section_lists(section: Any, path: str, fields: dict[str, Any]) -> None:
    if isinstance(section, dict):
        fields[f"{path}.cards"] = section.get("cards")
        for i, card in enumerate(_items(section.get("cards"))):
            _collect_card_lists(card, f"{path}.cards[{i}]", fields)


def _collect_view_lists(view: Any, path: str, fields: dict[str, Any]) -> None:
    if not isinstance(view, dict):
        return
    for key in ("cards", "sections", "badges"):
        fields[f"{path}.{key}"] = view.get(key)
    for i, card in enumerate(_items(view.get("cards"))):
        _collect_card_lists(card, f"{path}.cards[{i}]", fields)
    for i, section in enumerate(_items(view.get("sections"))):
        _collect_section_lists(section, f"{path}.sections[{i}]", fields)


_LIST_ITEM_COLLECTORS = {
    "views": _collect_view_lists,
    "sections": _collect_section_lists,
    "cards": _collect_card_lists,
}


def reject_malformed_dashboard_lists(
    config: dict[str, Any],
    url_path: str,
    *,
    source: Literal["config", "patch", "python_transform"] = "config",
) -> None:
    """Check the list positions the frontend types as arrays (issue #2548).

    Home Assistant saves any value here, and the frontend calls ``.map`` on it.
    """
    views = config.get("views")
    fields: dict[str, Any] = {"views": views}
    for i, view in enumerate(_items(views)):
        _collect_view_lists(view, f"views[{i}]", fields)
    reject_malformed_list_fields(
        fields, tuple(fields), {"url_path": url_path}, source=source
    )


def reject_malformed_dashboard_config(config: Any, url_path: str) -> None:
    """Check a replacement config, whether it arrived as a dict or a JSON string."""
    try:
        parsed = loads_if_json_container_str(config)
    except ValueError:
        return  # Malformed JSON is reported by the tool's own config parse.
    if isinstance(parsed, dict):
        reject_malformed_dashboard_lists(parsed, url_path)


# JSON Patch paths whose value has a known dashboard shape: a typed list, or one item of it.
_PATCH_LIST_PATH = re.compile(
    r"/(views)|/views/\d+/(cards|sections|badges)|/views/\d+/sections/\d+/(cards)"
)
# Paths inside a card: whether ``cards`` there is a core list depends on the card type.
_PATCH_INSIDE_CARD_PATH = re.compile(r"/views/\d+/(?:sections/\d+/)?cards/\d+/.+")
_PATCH_ITEM_PATH = re.compile(
    r"/(views)/(?:\d+|-)|/views/\d+/(sections)/(?:\d+|-)"
    r"|/views/\d+/(?:sections/\d+/)?(cards)/(?:\d+|-)"
)


def patch_writes_inside_card(patch: list[dict[str, Any]]) -> bool:
    """Whether an add/replace inside a card could carry a list field.

    Such ops need the stored config: only the parent card's type says whether
    ``cards`` there is a core list. Scalar edits (an icon, a name) never do.
    """
    return any(
        isinstance(op, dict)
        and op.get("op") in ("add", "replace")
        and _PATCH_INSIDE_CARD_PATH.fullmatch(path := str(op.get("path", "")))
        and (
            isinstance(op.get("value"), (dict, list))
            or path.rsplit("/", 1)[-1] == "cards"
        )
        for op in patch
    )


def reject_malformed_dashboard_patch(
    patch: list[dict[str, Any]], url_path: str
) -> None:
    """Apply the list check to add/replace values before the patch is applied."""
    fields: dict[str, Any] = {}
    for n, op in enumerate(patch):
        if not isinstance(op, dict) or op.get("op") not in ("add", "replace"):
            continue
        path, value = str(op.get("path", "")), op.get("value")
        label = f"patch[{n}].value ({path})"
        if path == "" and isinstance(value, dict):
            fields[f"{label}.views"] = value.get("views")
            for i, view in enumerate(_items(value.get("views"))):
                _collect_view_lists(view, f"{label}.views[{i}]", fields)
        elif match := _PATCH_LIST_PATH.fullmatch(path):
            kind = next(k for k in match.groups() if k)
            fields[label] = value
            collect = _LIST_ITEM_COLLECTORS.get(kind)
            if collect is not None:
                for i, item in enumerate(_items(value)):
                    collect(item, f"{label}[{i}]", fields)
        elif match := _PATCH_ITEM_PATH.fullmatch(path):
            kind = next(k for k in match.groups() if k)
            _LIST_ITEM_COLLECTORS[kind](value, label, fields)
    reject_malformed_list_fields(
        fields, tuple(fields), {"url_path": url_path, "action": "patch"}, source="patch"
    )
