"""Integration diagnostics fetch, projection and pagination helpers."""

import json
import logging
from typing import Any

from ..client.rest_client import (
    NON_ADMIN_TOKEN_WARNING,
    HomeAssistantAdminRequiredError,
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)

logger = logging.getLogger(__name__)


DIAGNOSTICS_DEFAULT_TIMEOUT_SECONDS = 60.0


def parse_diagnostics_fields(value: list[str] | str | None) -> list[str] | None:
    """Normalise the ``diagnostics_fields`` MCP-tool parameter to ``list[str] | None``.

    Accepts a native list of strings, a JSON-encoded list (e.g.
    ``'["home_assistant","issues"]'``), or a comma-separated string
    (e.g. ``'home_assistant, issues'``). Empty / whitespace-only input
    coerces to ``None`` so the diagnostics helper skips the projection.

    Raises:
        ValueError: when the input is not parseable as a list of strings —
            specifically: JSON that fails to decode, JSON that decodes to a
            non-list value (object, scalar), or any non-(list/str/None) type.
    """
    if value is None:
        return None
    if isinstance(value, list):
        parsed = [str(v).strip() for v in value if str(v).strip()]
        return parsed or None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.startswith("["):
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"diagnostics_fields must be a valid JSON list, got '{stripped}': {e}"
                ) from e
            if not isinstance(decoded, list):
                raise ValueError(
                    f"diagnostics_fields JSON must decode to a list, got {type(decoded).__name__}"
                )
            parsed = [str(v).strip() for v in decoded if str(v).strip()]
            return parsed or None
        if stripped.startswith("{"):
            raise ValueError(
                "diagnostics_fields must decode to a list, got a JSON object"
            )
        parsed = [p.strip() for p in stripped.split(",") if p.strip()]
        return parsed or None
    raise ValueError(
        f"diagnostics_fields must be list, string, or None; got {type(value).__name__}"
    )


async def _fetch_raw_diagnostics(
    client: Any,
    endpoint: str,
    timeout_seconds: float,
    device_id: str | None,
    result: dict[str, Any],
) -> None:
    """Fetch diagnostics from HA, populating result['data'] or result['error']."""
    try:
        result["data"] = await client._request("GET", endpoint, timeout=timeout_seconds)
    except HomeAssistantAdminRequiredError as e:
        result["error"] = f"{e}. {NON_ADMIN_TOKEN_WARNING}"
    except HomeAssistantAuthError as e:
        logger.warning("Diagnostics fetch auth error: %s", e)
        result["error"] = (
            "Authentication failed for diagnostics endpoint (HTTP 401): the "
            "configured access token is invalid or expired, or belongs to a "
            "non-admin user. Use a long-lived access token from an "
            "administrator's profile."
        )
    except HomeAssistantAPIError as e:
        status = getattr(e, "status_code", None)
        if status == 404:
            scope = "device" if device_id else "config entry"
            result["error"] = (
                f"Diagnostics not available for this {scope}: integration may "
                "not implement the diagnostics platform, or the id is invalid. "
                "Verify via ha_get_integration()."
            )
            logger.debug("Diagnostics not available (404): %s", e)
        elif status == 403:
            result["error"] = (
                "Diagnostics endpoint refused the request (HTTP 403): a reverse "
                "proxy blocked it, or Home Assistant's http.ban has banned this "
                "host's IP."
            )
            logger.warning("Diagnostics fetch refused (403): %s", e)
        else:
            result["error"] = (
                f"Diagnostics fetch failed (HTTP {status or '<status>'}): {e}"
            )
            logger.warning("Diagnostics fetch API error: %s", e)
    except HomeAssistantConnectionError as e:
        msg = str(e)
        if "timeout" in msg.lower():
            result["error"] = (
                f"Diagnostics fetch timed out after {timeout_seconds:.1f}s "
                "(ZHA dumps on large networks can exceed this; the integration "
                "may be too slow to return diagnostics on this network)"
            )
        else:
            result["error"] = f"Diagnostics fetch connection failed: {e}"
        logger.warning("Diagnostics fetch connection error: %s", e)
    except Exception as e:  # noqa: BLE001  # pragma: no cover - defensive last-resort guard
        logger.warning(
            "Diagnostics fetch unexpected error: %s: %s", type(e).__name__, e
        )
        result["error"] = f"Diagnostics fetch failed: {e}"


async def fetch_integration_diagnostics(
    client: Any,
    config_entry_id: str | None,
    device_id: str | None = None,
    *,
    timeout_seconds: float = DIAGNOSTICS_DEFAULT_TIMEOUT_SECONDS,
    fields: list[str] | None = None,
    truncate_at_bytes: int | None = None,
    data_path: str | None = None,
    data_offset: int = 0,
    data_limit: int | None = None,
) -> dict[str, Any]:
    """Get the integration diagnostics dump from HA's diagnostics REST endpoint.

    Hits ``GET /api/diagnostics/config_entry/{entry_id}`` (or the device-scoped
    variant when ``device_id`` is set). Requires a valid admin-scope token —
    401 surfaces as invalid/expired token, 403 as insufficient scope. Same
    artifact users grab via Settings → Devices & Services → [integration] → ⋯
    → Download diagnostics.

    Returns an embeddable sub-dict so callers can attach it to a larger response
    without raising on diagnostics-specific failures (matches the
    ``_fetch_repairs`` / ``_fetch_zha_network`` convention in
    ``tools_system.py``). The ``DIAGNOSTICS_DEFAULT_TIMEOUT_SECONDS`` default
    covers slow integrations like ZHA on large networks.

    Args:
        client: REST client exposing an async ``_request(method, endpoint,
            *, timeout)`` method that returns the decoded JSON body.
        config_entry_id: Config-entry id of the integration. Required;
            ``None`` / empty string short-circuits with a structured error
            sub-dict and no backend call.
        device_id: Optional device id under the entry. When set, switches
            the endpoint to the device-scoped variant.
        timeout_seconds: Per-request timeout (default
            ``DIAGNOSTICS_DEFAULT_TIMEOUT_SECONDS`` = 60.0s). ZHA dumps on
            large networks can run 30-60s, so the default is generous.
        fields: Optional list of top-level keys to keep from the integration's
            ``data`` payload (e.g. ``["home_assistant", "issues"]`` for Hue).
            Trims the payload before it hits the LLM context budget. Unknown
            keys are silently dropped; an ``omitted_fields`` list surfaces
            which requested keys weren't present. Only applies when ``data``
            is a dict. Applied before ``data_path``.
        truncate_at_bytes: Optional byte cap on the serialized resolved value
            (the post-``fields``/``data_path`` payload, or its paginated
            ``items`` when pagination applies). On hit, drops ``data`` /
            ``items`` and emits ``truncated: True``, ``bytes_total: <actual>``,
            ``byte_cap: <cap>``, plus ``available_fields: <top-level keys>``
            (when the capped value is a dict) so the model knows which
            ``fields`` or sub-path to request on the next call. Applied last.
        data_path: Optional dotted path into ``data`` to walk into a sub-tree
            (e.g. ``"data.devices"``, ``"home_assistant.version"``). Resolution
            failures (missing key, non-traversable value) replace ``data`` with
            ``null`` and add ``data_path_error`` to the result. When the
            resolved value is a list and ``data_limit`` is set, pagination
            applies — see ``data_limit``. Applied after ``fields``.
        data_offset: Pagination start index for list-valued ``data_path``
            results (default ``0``). Ignored when ``data_path`` is unset or
            ``data_limit`` is unset, or the resolved value is not a list.
        data_limit: Pagination window size for list-valued ``data_path``
            results. When set with a list-resolved path, swaps ``data`` for
            a pagination envelope ``{"path", "items", "offset", "limit",
            "total", "has_more"}``. Default ``None`` (return the full
            resolved value).
    """
    result: dict[str, Any] = {"config_entry_id": config_entry_id}
    if device_id:
        result["device_id"] = device_id

    if not config_entry_id:
        result["error"] = (
            "config_entry_id is required for diagnostics fetch. "
            "Use ha_get_integration() to find the config_entry_id for the "
            "integration."
        )
        return result

    endpoint = f"/diagnostics/config_entry/{config_entry_id}"
    if device_id:
        endpoint += f"/device/{device_id}"

    await _fetch_raw_diagnostics(client, endpoint, timeout_seconds, device_id, result)

    if "data" in result and result["data"] is None and "error" not in result:
        # Empty response body — distinct from an explicit error or a
        # cap-driven drop. Surface it as an error so callers don't confuse
        # ``{"data": null}`` with a successful zero-payload fetch.
        result["error"] = "Diagnostics endpoint returned an empty body"
        del result["data"]

    if "data" in result:
        _project_cap_and_paginate_diagnostics(
            result, fields, truncate_at_bytes, data_path, data_offset, data_limit
        )

    return result


def _apply_data_path_resolution(
    result: dict[str, Any],
    data: Any,
    data_path: str,
    data_limit: int | None,
    data_offset: int,
) -> tuple[Any, bool]:
    """Walk data_path, apply pagination, and update result. Returns (data, paginated).

    Called only when data_path is set; when absent the caller handles the
    orphan data_offset warning directly.
    """
    resolved, path_error = _resolve_data_path(data, data_path)
    if path_error is not None:
        result["data"] = None
        result["data_path_error"] = path_error
        return None, False

    result["data_path"] = data_path
    if isinstance(resolved, list) and data_limit is not None:
        total = len(resolved)
        start = max(0, data_offset)
        end = start + data_limit
        items = resolved[start:end]
        page: dict[str, Any] = {
            "path": data_path,
            "items": items,
            "offset": start,
            "limit": data_limit,
            "total": total,
            "has_more": end < total,
        }
        result["data"] = page
        return items, True

    # Pagination intent has nowhere to apply: either data_limit is set but the
    # resolved value isn't a list, or data_offset is set without data_limit.
    if data_limit is not None:
        type_name = "null" if resolved is None else type(resolved).__name__
        result["data_pagination_warning"] = (
            f"data_limit ignored: resolved value at '{data_path}' is {type_name}, not a list"
        )
    elif data_offset > 0:
        result["data_pagination_warning"] = (
            "data_offset ignored: data_limit not set (no pagination window to slice)"
        )
    result["data"] = resolved
    return resolved, False


def _apply_truncation_cap(
    result: dict[str, Any],
    data: Any,
    truncate_at_bytes: int | None,
    paginated: bool,
) -> None:
    if truncate_at_bytes is None or data is None:
        return
    try:
        serialized = json.dumps(data, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return
    bytes_total = len(serialized.encode("utf-8"))
    if bytes_total > truncate_at_bytes:
        result["truncated"] = True
        result["bytes_total"] = bytes_total
        result["byte_cap"] = truncate_at_bytes
        if paginated:
            envelope = result["data"]
            preserved = {k: v for k, v in envelope.items() if k != "items"}
            preserved["truncated"] = True
            result["data"] = preserved
        else:
            if isinstance(data, dict):
                result["available_fields"] = sorted(data.keys())
            del result["data"]


def _project_cap_and_paginate_diagnostics(
    result: dict[str, Any],
    fields: list[str] | None,
    truncate_at_bytes: int | None,
    data_path: str | None,
    data_offset: int,
    data_limit: int | None,
) -> None:
    """Apply field projection, data_path walk, optional pagination, then byte
    cap. Mutates ``result`` (adds keys, may replace or delete
    ``result["data"]``). When pagination produced an envelope and the cap
    fires, the envelope's metadata (``path``, ``offset``, ``limit``,
    ``total``, ``has_more``) is preserved sans ``items`` so the caller can
    issue a narrower follow-up; the unpaginated case drops ``data`` entirely.

    See ``fetch_integration_diagnostics`` for the public contract.
    """
    data = result.get("data")

    if fields and isinstance(data, dict):
        kept = {k: data[k] for k in fields if k in data}
        # Dedup caller-supplied duplicates while preserving order.
        omitted = list(dict.fromkeys(k for k in fields if k not in data))
        result["data"] = kept
        if omitted:
            result["omitted_fields"] = omitted
        data = kept

    # Whitespace-only (or empty) paths normalize to "unset"; surface a warning
    # so callers can tell their intent was swallowed instead of resolving
    # silently. The earlier whitespace branch nulls ``data_path``, so the
    # ``elif data_offset > 0`` branch below is guarded against clobbering
    # this warning when both inputs land together.
    if data_path is not None and not data_path.strip():
        result["data_pagination_warning"] = (
            "data_path ignored: value was empty or whitespace-only"
        )
        data_path = None

    paginated = False
    if data_path:
        data, paginated = _apply_data_path_resolution(
            result, data, data_path, data_limit, data_offset
        )
    elif data_offset > 0 and "data_pagination_warning" not in result:
        # ``data_offset`` set without ``data_path`` — the resolver branch is
        # skipped entirely, so the offset has no effect on the response.
        # Mirrors the orphan-warning gates at the tool layer. Guarded so the
        # whitespace-path warning above isn't clobbered when both inputs
        # land together (the whitespace input nulled ``data_path``, dropping
        # us into this elif; the earlier warning takes precedence).
        result["data_pagination_warning"] = (
            "data_offset ignored: data_path not set (no resolved sub-tree to paginate)"
        )

    _apply_truncation_cap(result, data, truncate_at_bytes, paginated)


def _resolve_data_path(data: Any, path: str) -> tuple[Any, str | None]:
    """Walk ``data`` along the dotted ``path`` and return ``(value, error)``.

    Returns ``(value, None)`` on success or ``(None, error_message)`` when
    a segment can't be resolved (missing key, descent into non-dict / null,
    empty path). List indices are not supported: address a list-valued
    sub-tree by name and let the caller's pagination kwargs (``data_offset``
    / ``data_limit``) slice it. Index-segment support is a candidate
    follow-up.

    Limitation: dotted keys (e.g. ``sensor.zha_temp_42``, MQTT-style topics
    containing a literal ``.``) are not addressable — the path is split on
    ``.`` without escape support. Workaround: omit ``data_path`` and walk
    the returned payload in the caller. Escape syntax is a candidate
    follow-up.
    """
    if not path or not path.strip():
        return None, "data_path must be a non-empty dotted path"
    segments = path.split(".")
    current: Any = data
    walked: list[str] = []
    for seg in segments:
        if not seg:
            return None, (
                f"data_path '{path}' has an empty segment (after '{'.'.join(walked)}')"
            )
        if current is None:
            return None, (
                f"data_path '{path}' resolved to null at "
                f"'{'.'.join(walked) or '<root>'}' — "
                "sub-tree not present in this payload"
            )
        if not isinstance(current, dict):
            return None, (
                f"data_path '{path}' cannot descend into "
                f"{type(current).__name__} at '{'.'.join(walked) or '<root>'}'"
            )
        if seg not in current:
            available = sorted(current.keys()) if isinstance(current, dict) else []
            # Only mention the dotted-key limitation when the available keys
            # at this level actually contain one — a plain typo (e.g.
            # ``data.versionz``) shouldn't be told its ``.`` is being
            # mis-parsed as a separator when no sibling key has a ``.`` in it.
            ambiguous = any(isinstance(k, str) and "." in k for k in available)
            if ambiguous:
                hint = (
                    "; note: a sibling key here contains a literal '.' which "
                    "is not addressable via data_path — omit data_path and "
                    "walk the returned payload in the caller"
                )
            else:
                hint = ""
            return None, (
                f"data_path '{path}' missing key '{seg}' at "
                f"'{'.'.join(walked) or '<root>'}' "
                f"(available: {available}{hint})"
            )
        current = current[seg]
        walked.append(seg)
    return current, None
