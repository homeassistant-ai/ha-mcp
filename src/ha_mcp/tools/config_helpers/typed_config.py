"""ha_config_set_helper's ``config`` argument folded into the typed fields."""

import contextlib
import functools
import inspect
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from ...errors import ErrorCode, create_error_response
from ..component_helper_collections import fetch_helper_schemas
from ..helpers import clear_or_keep, hidden_param_names, raise_tool_error
from .schemas import (
    _CORE_HELPER_SCHEMAS,
    _TYPE_TYPED_PARAMS,
    SIMPLE_HELPER_TYPES,
    _simple_helper_error_context,
)
from .validation import _validate_applicable_params


@functools.cache
def _simple_config_model() -> type[BaseModel]:
    """Validate SIMPLE-helper `config` keys exactly as the hidden tool params are."""
    from ..tools_config_helpers import HelperConfigTools  # the tool imports this module

    tool_fn = HelperConfigTools.ha_config_set_helper
    params = inspect.signature(tool_fn).parameters
    # name/icon are top-level params, but HA's own schemas list them too.
    keys = hidden_param_names(tool_fn) | {"name", "icon"}
    fields: dict[str, Any] = {name: (params[name].annotation, None) for name in keys}
    return create_model(
        "SimpleHelperConfig", __config__=ConfigDict(extra="forbid"), **fields
    )


def _merge_simple_helper_config(
    helper_type: str, config: Any, values: dict[str, Any]
) -> dict[str, Any]:
    """Fold SIMPLE-helper fields passed inside `config` into the typed params."""
    if config in (None, {}, ""):
        return values
    valid_keys = sorted(_TYPE_TYPED_PARAMS.get(helper_type, frozenset()) | {"name"})
    suggestions = [
        f"Valid config keys for {helper_type}: {', '.join(valid_keys)}",
        "area_id, labels and category are top-level parameters, not config keys",
    ]
    try:
        fields = _simple_config_model().model_validate(config)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(map(str, err['loc'])) or 'config'}: {err['msg']}"
            for err in e.errors()
        )
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid config for helper_type='{helper_type}': {problems}",
                context=_simple_helper_error_context(helper_type),
                suggestions=suggestions,
            )
        )
    merged = dict(values)
    for key in fields.model_fields_set:
        value = getattr(fields, key)
        if value is None:
            continue
        if values[key] is not None and values[key] != value:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"'{key}' was passed both as a parameter and in config with "
                    "different values.",
                    context=_simple_helper_error_context(helper_type),
                    suggestions=[f"Pass '{key}' once, inside config"],
                )
            )
        merged[key] = value
    return merged


def _prepare_typed_params(
    helper_type: str,
    config: Any,
    name: str | None,
    icon: str | None,
    type_kw: dict[str, Any],
) -> tuple[str | None, str | None, dict[str, Any]]:
    """Fold a SIMPLE helper's `config` into its typed params, then reject
    params that don't apply to the type (Bug 4b/7c/10/14, issue #1150)."""
    if helper_type in SIMPLE_HELPER_TYPES:
        merged = _merge_simple_helper_config(
            helper_type, config, {"name": name, "icon": icon, **type_kw}
        )
        name, icon = merged.pop("name"), merged.pop("icon")
        icon = clear_or_keep(icon, "icon")  # a config icon too
        type_kw = merged
    _validate_applicable_params(helper_type, {"icon": icon, **type_kw})
    return name, icon, type_kw


@contextlib.asynccontextmanager
async def _core_schema_context(
    client: Any, helper_type: str, action: str | None
) -> AsyncIterator[None]:
    """Serve Core's field lists to validation errors raised inside the block."""
    token = None
    if helper_type in SIMPLE_HELPER_TYPES:
        schemas = await fetch_helper_schemas(client)
        if schemas:
            token = _CORE_HELPER_SCHEMAS.set((schemas, action or "create"))
    try:
        yield
    finally:
        if token is not None:
            _CORE_HELPER_SCHEMAS.reset(token)
