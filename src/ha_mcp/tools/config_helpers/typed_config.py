"""Core's storage-helper field lists, served to one ha_config_set_helper call."""

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from ..component_helper_collections import fetch_helper_schemas
from .schemas import (
    _CORE_HELPER_SCHEMAS,
    SIMPLE_HELPER_TYPES,
)


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
