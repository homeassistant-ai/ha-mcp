"""The dev group installs the integration libraries Core's manifests pin.

Unit tests run Core's own Thread, OTBR, Z-Wave JS, ZHA, KNX, Local Calendar
and Remote Calendar handlers, so the dev group installs their requirements. A
Core update that moves a manifest pin must move the dev pin with it, or the
tests run a library that Core version never ships with.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import homeassistant.components
from packaging.requirements import Requirement

_REPO = Path(__file__).resolve().parents[3]
_COMPONENTS = Path(homeassistant.components.__file__).parent
_INTEGRATIONS = (
    "otbr",
    "thread",
    "zwave_js",
    "zha",
    "usb",
    "hassio",
    "homeassistant_hardware",
    "infrared",
    "knx",
    "local_calendar",
    "remote_calendar",
)


def _manifest_pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for domain in _INTEGRATIONS:
        manifest = json.loads((_COMPONENTS / domain / "manifest.json").read_text())
        for requirement in manifest.get("requirements", []):
            parsed = Requirement(requirement)
            pins[parsed.name.lower()] = str(parsed.specifier)
    return pins


def _dev_pins() -> dict[str, str]:
    project = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    pins: dict[str, str] = {}
    for entry in project["dependency-groups"]["dev"]:
        if isinstance(entry, str):
            parsed = Requirement(entry)
            pins[parsed.name.lower()] = str(parsed.specifier)
    return pins


def test_every_integration_requirement_is_pinned_as_core_pins_it() -> None:
    manifest = _manifest_pins()
    dev = _dev_pins()

    assert {name: dev.get(name) for name in manifest} == manifest
