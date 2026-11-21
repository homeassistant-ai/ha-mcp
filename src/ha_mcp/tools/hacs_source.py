"""The Marketplace's download source: the options flow of its config entry.

Core's Marketplace options flow is one form whose select names the download
profiles, the panel's "Download source". The read describes that form with
``ha_get_integration``'s options-form read; the write answers it one step per
call through ``ha_set_integration``'s options entry point, then waits out the
reload Core runs once the saved source changed and reads the form again, as
Core's answer to a finished options flow does not carry what it saved.
"""

import contextlib
from typing import Any

from .config_entry_reload_watch import watch_entry_reload
from .hacs_backend import marketplace_entry
from .integration_flow_steps import run_integration_step
from .integration_schema import fetch_options_schema

_READ = {"action": "download_source"}
_WRITE = {"action": "set_download_source"}


async def _options_form(client: Any, entry: dict[str, Any]) -> dict[str, Any]:
    """The entry's options form as ``options_schema``, with any ``warnings``."""
    # The entry names the domain whose strings hold Core's help text.
    resp: dict[str, Any] = {"entry": entry}
    await fetch_options_schema(client, str(entry["entry_id"]), resp)
    del resp["entry"]
    return resp


async def download_source(ws_client: Any, client: Any) -> dict[str, Any]:
    entry = await marketplace_entry(ws_client, _READ)
    return {
        **_READ,
        "entry_id": entry["entry_id"],
        **await _options_form(client, entry),
    }


async def set_download_source(
    ws_client: Any,
    *,
    client: Any,
    config: dict[str, Any] | None,
    flow_id: str | None,
    abort: bool | None,
) -> dict[str, Any]:
    entry = await marketplace_entry(ws_client, _WRITE)
    entry_id = str(entry["entry_id"])
    submits = not abort and (flow_id is not None or config is not None)
    # Subscribed before the submit: Core queues the reload and returns, so
    # only a stream opened first sees it settle.
    watch = watch_entry_reload(client, entry_id) if submits else None
    async with watch or contextlib.nullcontext() as reloaded:
        step = await run_integration_step(
            client,
            domain=None,
            entry_id=entry_id,
            flow_id=flow_id,
            config=config,
            menu_choice=None,
            abort=bool(abort),
        )
        answer = {**_WRITE, **{k: v for k, v in step.items() if k != "success"}}
        if reloaded is None or not step.get("updated"):
            return answer
        settled = await reloaded()
    saved = await _options_form(client, entry)
    warnings = saved.get("warnings") or []
    state = settled.get("state") if settled else None
    if settled is None:
        warnings.append(
            "No settled reload of the Marketplace was observed. Home Assistant "
            "reloads it only when the saved source differs from the one before; "
            "the reload may also not have been visible or not settled within "
            "the wait. Settings > Devices & services > Marketplace shows its "
            "state."
        )
    elif state != "loaded":
        warnings.append(
            f"The Marketplace reloaded with the saved source and is now "
            f"'{state}', not loaded; the Home Assistant log says why."
        )
    return {
        **answer,
        "entry_state": state,
        "options_schema": saved.get("options_schema"),
        "message": "Home Assistant saved the download source; options_schema "
        "is the form read back from it, with the saved choice as current.",
        **({"warnings": warnings} if warnings else {}),
    }
