"""Core set up from YAML files in its config directory.

Core's config API reads and writes ``scenes.yaml``, ``automations.yaml`` and
``scripts.yaml`` there, a reload reads ``configuration.yaml`` again, and
blueprints load from ``blueprints/``. A test that uses these overrides
``hass_config_dir`` with ``hass_tmp_config_dir`` so the files are its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from homeassistant import config as conf_util
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import dump


async def write_config_files(hass: HomeAssistant, files: dict[str, str]) -> None:
    """Write ``files`` (path relative to the config directory: text)."""
    config_dir = Path(hass.config.config_dir)

    def write() -> None:
        for name, text in files.items():
            path = config_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    await hass.async_add_executor_job(write)


async def setup_from_configuration(hass: HomeAssistant, *domains: str) -> None:
    """Set ``domains`` up from ``configuration.yaml``, then Core's config API."""
    config = await conf_util.async_hass_config_yaml(hass)
    for domain in domains:
        assert await async_setup_component(hass, domain, config)
    assert await async_setup_component(hass, "config", {})
    await hass.async_block_till_done()


async def setup_scenes(
    hass: HomeAssistant,
    stored: list[dict[str, Any]],
    *,
    yaml_only: list[dict[str, Any]] | None = None,
) -> None:
    """Set up scenes: ``stored`` in ``scenes.yaml``, the file Core's config API
    edits, and ``yaml_only`` in ``configuration.yaml``, which it cannot read."""
    configuration = "scene: !include scenes.yaml\n"
    if yaml_only:
        indented = "".join(f"  {line}\n" for line in dump(yaml_only).splitlines())
        configuration += f"scene yaml:\n{indented}"
    await write_config_files(
        hass, {"scenes.yaml": dump(stored), "configuration.yaml": configuration}
    )
    await setup_from_configuration(hass, "scene")
