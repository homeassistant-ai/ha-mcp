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
    SIMPLE_HELPER_TYPES,
    _simple_helper_error_context,
)
from .validation import _reject_storage_params_on_flow_helper


@functools.cache
def _simple_config_model() -> type[BaseModel]:
    """Coerce known SIMPLE-helper `config` keys exactly as the hidden tool params
    are; other keys are kept for Core, which rejects them and suggests the
    closest field."""
    from ..tools_config_helpers import HelperConfigTools  # the tool imports this module

    tool_fn = HelperConfigTools.ha_config_set_helper
    params = inspect.signature(tool_fn).parameters
    # name/icon are top-level params, but HA's own schemas list them too.
    keys = hidden_param_names(tool_fn) | {"name", "icon"}
    fields: dict[str, Any] = {name: (params[name].annotation, None) for name in keys}
    return create_model(
        "SimpleHelperConfig", __config__=ConfigDict(extra="allow"), **fields
    )


def _merge_simple_helper_config(
    helper_type: str, config: Any, values: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fold SIMPLE-helper fields passed inside `config` into the typed params.

    Returns the merged typed values and the keys the tool does not declare,
    which go to Core unchanged.
    """
    if config in (None, {}, ""):
        return values, {}
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
                suggestions=[
                    f"ha_config_list_helpers({helper_type!r}, describe=True) lists "
                    "the fields",
                    "area_id, labels and category are top-level parameters, not "
                    "config keys",
                ],
            )
        )
    merged = dict(values)
    for key in fields.model_fields_set - set(fields.model_extra or {}):
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
    return merged, dict(fields.model_extra or {})


def _prepare_typed_params(
    helper_type: str,
    config: Any,
    name: str | None,
    icon: str | None,
    type_kw: dict[str, Any],
) -> tuple[str | None, str | None, dict[str, Any], dict[str, Any]]:
    """Fold a SIMPLE helper's `config` into its typed params.

    Returns name, icon, the typed values and the undeclared `config` keys.
    A flow helper takes its fields in `config`, so storage-helper parameters
    passed top-level are rejected for it.
    """
    if helper_type not in SIMPLE_HELPER_TYPES:
        _reject_storage_params_on_flow_helper(helper_type, type_kw)
        return name, icon, type_kw, {}
    merged, passthrough = _merge_simple_helper_config(
        helper_type, config, {"name": name, "icon": icon, **type_kw}
    )
    name, icon = merged.pop("name"), merged.pop("icon")
    icon = clear_or_keep(icon, "icon")  # a config icon too
    return name, icon, merged, passthrough


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
