"""Shared helpers for the ``llm_api`` unit tests (issue #1745)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm


def llm_context() -> llm.LLMContext:
    """The context a conversation agent hands Core's LLM API."""
    return llm.LLMContext(
        platform="test",
        context=None,
        language="en",
        assistant="conversation",
        device_id=None,
    )


def registered_apis(hass: HomeAssistant) -> dict[str, Any]:
    """The LLM APIs registered in Core's registry for ``hass``, by id."""
    return {api.id: api for api in llm.async_get_apis(hass)}
