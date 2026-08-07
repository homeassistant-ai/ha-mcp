"""Schema-driven secret redaction behind the ``redact_secrets`` flag (#2157).

When ``Settings.redact_secrets`` is on (``redact_secrets`` addon option,
``REDACT_SECRETS`` env var, or the Server Settings web-UI toggle), three
surfaces stop returning operator secrets verbatim:

1. **Add-on options** (``ha_get_addon`` detail form, ``ha_manage_addon``'s
   proxy-mode 403 hints): every ``options`` value whose Supervisor-serialized
   ``schema`` entry carries ``format: "password"`` is replaced with a
   set/empty sentinel. The schema is authoritative — keys without a schema
   entry are never touched, and no name-based guessing happens.
2. **Integration options** (``ha_get_integration``): fields the
   options-flow schema marks with a password text selector are replaced the
   same way, on both the harvested options and the ``include_schema=True``
   schema echo (whose ``suggested_value`` carries the live value).
3. **Every other tool response**: password values seen while serving 1 and 2
   are remembered in a process-local known-secrets set, and
   :class:`RedactSecretsMiddleware` (registered innermost) scrubs any
   occurrence in any tool response or tool error to ``<redacted>``.

The set/empty sentinel split keeps the genuinely useful diagnostic — "is
this credential configured at all?" — answerable without disclosing the
value. ``ha_manage_addon`` rejects submitted option values equal to a
sentinel so a redacted marker can never be round-tripped into a live add-on
config (writes still merge from the real, unredacted current options
server-side).

With the flag off (the default) every code path here is a passthrough and
tool output is byte-identical to the legacy behavior.
"""

from __future__ import annotations

import copy
import logging
import threading
from collections.abc import Collection, Iterable
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext

from .config import get_global_settings

logger = logging.getLogger(__name__)

# Sentinels emitted in place of secret values. SET/EMPTY preserve the
# "configured at all?" diagnostic on schema-redacted surfaces; KNOWN marks a
# known-value scrub hit inside arbitrary tool output.
REDACTED_SET = "<redacted: set>"
REDACTED_EMPTY = "<redacted: empty>"
REDACTED_KNOWN = "<redacted>"

_SENTINELS = frozenset({REDACTED_SET, REDACTED_EMPTY, REDACTED_KNOWN})

# Values shorter than this are never registered for the known-value text
# scrub: substring-replacing a tiny fragment (think a 4-char PIN that also
# appears as a port number or entity-id fragment) would corrupt unrelated
# output far more often than it would protect anything.
_MIN_KNOWN_SECRET_LEN = 6

_known_secrets: set[str] = set()
_known_secrets_lock = threading.Lock()


def redaction_enabled() -> bool:
    """Live per-request read of the ``redact_secrets`` flag."""
    return bool(get_global_settings().redact_secrets)


def sentinel_for(value: Any) -> str:
    """SET/EMPTY sentinel preserving the "configured at all?" diagnostic."""
    if value is None or value in ("", []):
        return REDACTED_EMPTY
    return REDACTED_SET


def is_sentinel(value: Any) -> bool:
    """True when ``value`` is exactly one of the redaction sentinels."""
    return isinstance(value, str) and value in _SENTINELS


# ---------------------------------------------------------------------------
# Surface 1: Supervisor add-on options + schema
# ---------------------------------------------------------------------------

# Supervisor serializes an add-on ``schema`` as a list of ``{name, type, ...}``
# field descriptors; a nested option group is an entry whose own ``schema``
# key holds another such list, and a password leaf carries
# ``format: "password"`` (e.g. ``{"name": "github_pat", "type": "string",
# "format": "password"}``).


def _iter_schema_entries(schema: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(schema, list):
        return
    for entry in schema:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            yield entry


def redact_addon_options(options: Any, schema: Any) -> Any:
    """Copy of ``options`` with ``format: password`` values sentinel-replaced.

    Walks nested option groups recursively (a group's value may be a dict or,
    with ``multiple``, a list of dicts). Keys without a schema entry pass
    through untouched. Non-dict ``options`` are returned as-is.
    """
    if not isinstance(options, dict):
        return options
    out = dict(options)
    for entry in _iter_schema_entries(schema):
        name = entry["name"]
        if name not in out:
            continue
        value = out[name]
        nested = entry.get("schema")
        if isinstance(nested, list):
            if isinstance(value, dict):
                out[name] = redact_addon_options(value, nested)
            elif isinstance(value, list):
                out[name] = [
                    redact_addon_options(item, nested)
                    if isinstance(item, dict)
                    else item
                    for item in value
                ]
            continue
        if entry.get("format") == "password":
            out[name] = sentinel_for(value)
    return out


def collect_addon_secret_values(options: Any, schema: Any) -> set[str]:
    """Live ``format: password`` option values, for the known-value scrub."""
    values: set[str] = set()
    if not isinstance(options, dict):
        return values
    for entry in _iter_schema_entries(schema):
        value = options.get(entry["name"])
        nested = entry.get("schema")
        if isinstance(nested, list):
            if isinstance(value, dict):
                values |= collect_addon_secret_values(value, nested)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        values |= collect_addon_secret_values(item, nested)
            continue
        if entry.get("format") == "password":
            candidates = value if isinstance(value, list) else [value]
            values.update(item for item in candidates if isinstance(item, str) and item)
    return values


def sentinel_option_keys(options: Any, _prefix: str = "") -> list[str]:
    """Dotted paths of submitted option values that equal a redaction sentinel.

    Used by ``ha_manage_addon`` config mode to reject round-tripping a
    redacted read back into a live add-on config.
    """
    keys: list[str] = []
    if not isinstance(options, dict):
        return keys
    for name, value in options.items():
        path = f"{_prefix}.{name}" if _prefix else str(name)
        if is_sentinel(value):
            keys.append(path)
        elif isinstance(value, dict):
            keys.extend(sentinel_option_keys(value, path))
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                if is_sentinel(item):
                    keys.append(f"{path}[{idx}]")
                elif isinstance(item, dict):
                    keys.extend(sentinel_option_keys(item, f"{path}[{idx}]"))
    return keys


# ---------------------------------------------------------------------------
# Surface 2: options-flow schema (config entries)
# ---------------------------------------------------------------------------


def is_password_flow_field(field: dict[str, Any]) -> bool:
    """True when a serialized flow field is marked as a password input.

    The authoritative marker is a text selector with ``type: "password"``
    (``TextSelectorType.PASSWORD`` — serialized as
    ``{"selector": {"text": {"type": "password"}}}``). A literal
    ``type: "password"`` on the field is honored too. Fields with no
    password marker cannot be classified from metadata and pass through —
    the known-value scrub is the only remaining line for those.
    """
    selector = field.get("selector")
    if isinstance(selector, dict):
        text = selector.get("text")
        if isinstance(text, dict) and text.get("type") == "password":
            return True
    return field.get("type") == "password"


def redact_flow_schema(data_schema: Any) -> Any:
    """Deep-copied flow schema with password-field current values redacted.

    HA injects the persisted option into each field's
    ``description.suggested_value`` (and static defaults into ``default`` /
    ``value``), so an ``include_schema=True`` echo carries the live secret.
    Harvests each replaced value into the known-secrets set first.
    """
    if not isinstance(data_schema, list):
        return data_schema
    schema = copy.deepcopy(data_schema)
    _redact_flow_schema_in_place(schema)
    return schema


def _redact_flow_schema_in_place(data_schema: list[Any]) -> None:
    for field in data_schema:
        if not isinstance(field, dict):
            continue
        nested = field.get("schema")
        if isinstance(nested, list):
            _redact_flow_schema_in_place(nested)
            continue
        if not is_password_flow_field(field):
            continue
        description = field.get("description")
        if isinstance(description, dict) and "suggested_value" in description:
            value = description["suggested_value"]
            register_known_secret_values([value] if isinstance(value, str) else [])
            description["suggested_value"] = sentinel_for(value)
        for key in ("default", "value"):
            if key in field and field[key] is not None:
                value = field[key]
                register_known_secret_values([value] if isinstance(value, str) else [])
                field[key] = sentinel_for(value)


def redact_options_by_flow_schema(options: Any, data_schema: Any) -> Any:
    """Copy of ``options`` with password-flow-field values sentinel-replaced.

    For the component-served path, whose raw persisted options are populated
    independently of the flow probe: once the flow schema is in hand, the
    fields it marks as passwords are redacted here. Harvests replaced string
    values into the known-secrets set first.
    """
    if not isinstance(options, dict) or not isinstance(data_schema, list):
        return options
    # Local import: config_entry_flow_form imports tool helpers; keep this
    # module importable from config-adjacent contexts without dragging the
    # tools package in at module import time.
    from .tools.config_entry_flow_form import iter_schema_fields

    out = dict(options)
    for field in iter_schema_fields(data_schema):
        if not is_password_flow_field(field):
            continue
        name = field.get("name")
        if not isinstance(name, str) or name not in out:
            continue
        value = out[name]
        if isinstance(value, str) and value:
            register_known_secret_values([value])
        out[name] = sentinel_for(value)
    return out


# ---------------------------------------------------------------------------
# Surface 3: known-value scrub across all tool responses
# ---------------------------------------------------------------------------


def register_known_secret_values(values: Iterable[str]) -> None:
    """Remember live secret values for the global response scrub.

    Values shorter than ``_MIN_KNOWN_SECRET_LEN`` are dropped (substring
    replacement of tiny fragments corrupts unrelated output). The set is
    process-local and fills as the server fetches the payloads that carry
    the secrets; it is only consulted while ``redact_secrets`` is on.
    """
    filtered = {
        v for v in values if isinstance(v, str) and len(v) >= _MIN_KNOWN_SECRET_LEN
    }
    if not filtered:
        return
    with _known_secrets_lock:
        _known_secrets.update(filtered)


def known_secret_values() -> frozenset[str]:
    """Snapshot of the registered secret values."""
    with _known_secrets_lock:
        return frozenset(_known_secrets)


def _clear_known_secret_values() -> None:
    """Test hook: empty the known-secrets set."""
    with _known_secrets_lock:
        _known_secrets.clear()


def scrub_text(text: str, secrets: Collection[str]) -> str:
    """Replace every occurrence of each known secret with ``<redacted>``.

    Longest-first so a secret that is a substring of another cannot leave
    fragments behind.
    """
    for secret in sorted(secrets, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, REDACTED_KNOWN)
    return text


def scrub_obj(obj: Any, secrets: Collection[str]) -> Any:
    """Recursively scrub known secret values from a JSON-shaped object."""
    if isinstance(obj, str):
        return scrub_text(obj, secrets)
    if isinstance(obj, dict):
        return {
            scrub_obj(key, secrets): scrub_obj(value, secrets)
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [scrub_obj(item, secrets) for item in obj]
    return obj


class RedactSecretsMiddleware(Middleware):
    """Scrub known secret values from every tool response and tool error.

    Registered innermost (after the visibility outbound scan) so it rewrites
    the raw tool output; the visibility middleware only refuses or passes
    through, so ordering between the two is otherwise inert. Consults the
    live flag per call and is a passthrough while it is off or no secret
    values are known.
    """

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        try:
            result = await call_next(context)
        except ToolError as exc:
            if redaction_enabled():
                secrets = known_secret_values()
                if secrets and getattr(exc, "args", None):
                    exc.args = tuple(
                        scrub_text(arg, secrets) if isinstance(arg, str) else arg
                        for arg in exc.args
                    )
            raise
        if not redaction_enabled():
            return result
        secrets = known_secret_values()
        if not secrets:
            return result
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                scrubbed = scrub_text(text, secrets)
                if scrubbed != text:
                    block.text = scrubbed
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            result.structured_content = scrub_obj(structured, secrets)
        return result
