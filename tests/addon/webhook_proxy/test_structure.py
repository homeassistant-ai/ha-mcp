"""What the Supervisor and Core need from the app's package, per flavor."""

from __future__ import annotations

import ast
import json
from types import ModuleType
from typing import Any

import yaml
from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from ._start import SERVER_APP_DIRS
from ._variants import VARIANTS, Variant, load_start


def _config(variant: Variant) -> dict[str, Any]:
    config: dict[str, Any] = yaml.safe_load(
        (variant.addon_dir / "config.yaml").read_text()
    )
    return config


def _options_start_reads(variant: Variant) -> set[str]:
    """The option keys ``start.py`` reads from /data/options.json."""
    tree = ast.parse(variant.start_src())
    reader = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_read_addon_options"
    )
    return {
        str(call.args[0].value)
        for call in ast.walk(reader)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "get"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "config"
        and isinstance(call.args[0], ast.Constant)
    }


def test_supervisor_build_gets_the_files_it_copies(variant: Variant) -> None:
    """The Supervisor builds the app locally from its Dockerfile: a missing
    file, or a component not copied to the ``/opt`` path ``start.py`` installs
    it from, leaves the app unbuildable or unable to install the integration."""
    for name in ("config.yaml", "Dockerfile", "start.py", "DOCS.md"):
        assert (variant.addon_dir / name).is_file(), name
    assert (variant.component_dir / "strings.json").is_file()
    dockerfile = (variant.addon_dir / "Dockerfile").read_text()
    assert "COPY start.py /" in dockerfile
    assert f"COPY {variant.component} /opt/{variant.component}" in dockerfile
    assert "/start.py" in dockerfile.split("CMD", 1)[1]


def test_supervisor_builds_the_image_instead_of_pulling_one(variant: Variant) -> None:
    """No image is published for the proxy: an ``image`` key would make the
    Supervisor pull one that does not exist instead of building the Dockerfile."""
    assert "image" not in _config(variant)


async def test_core_loads_the_bundled_component_as_a_config_flow_integration(
    hass: HomeAssistant, component: ModuleType, variant: Variant
) -> None:
    """``start.py`` creates the entry through the config flow and the proxy
    serves through Core's webhook integration, so Core must find both."""
    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
    integration = await loader.async_get_integration(hass, variant.domain)

    assert integration.config_flow is True
    assert "webhook" in integration.dependencies
    result = await hass.config_entries.flow.async_init(
        variant.domain, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    hass.config_entries.flow.async_abort(result["flow_id"])


def test_app_has_the_supervisor_access_start_needs(variant: Variant) -> None:
    """``start.py`` lists apps and reads the server app's logs (Supervisor API
    with the manager role), calls Core's API, and writes the component and its
    config into /config. Without any of these grants the start fails."""
    config = _config(variant)
    for key in ("name", "description", "version", "slug", "arch"):
        assert key in config, key
    assert config["hassio_api"] is True
    assert config["hassio_role"] == "manager"
    assert config["homeassistant_api"] is True
    assert "config:rw" in config["map"]


def test_slug_is_the_one_the_sibling_flavor_refuses_to_run_beside(
    variant: Variant,
) -> None:
    """Only one flavor may run; each refuses when the other's slug is started.
    A slug the sibling does not look for would let both run and collide."""
    sibling = next(v for v in VARIANTS if v.key != variant.key)
    assert _config(variant)["slug"] == load_start(sibling).SIBLING_SLUG_BASE


def test_every_option_start_reads_is_in_the_schema_and_used(variant: Variant) -> None:
    """The Supervisor only writes options its schema declares, so an option
    ``start.py`` reads but the schema lacks can never be set; one the schema
    declares but ``start.py`` ignores does nothing for the user."""
    assert _options_start_reads(variant) == set(_config(variant)["schema"])


def test_default_port_is_the_server_apps_port(variant: Variant) -> None:
    """Discovery builds the target URL from ``mcp_port``; its default must be
    the port the ha-mcp server apps listen on."""
    default = _config(variant)["options"]["mcp_port"]
    for server_dir in SERVER_APP_DIRS:
        server = yaml.safe_load((server_dir / "config.yaml").read_text())
        assert f"{default}/tcp" in server["ports"], server_dir.name


def test_oauth_options_stay_hidden_and_the_secret_is_masked(variant: Variant) -> None:
    """OAuth options have no default, so the Supervisor hides them under
    "Show unused optional configuration options" and the no-auth setup keeps
    its plain configuration tab; the client secret is a password field."""
    config = _config(variant)
    oauth_options = [key for key in config["schema"] if "oauth" in key]
    assert "enable_oauth" in oauth_options
    for key in oauth_options:
        assert key not in config["options"], key
        assert str(config["schema"][key]).endswith("?"), key
    assert config["schema"]["oauth_client_secret"] == "password?"


def test_inbound_request_logging_is_off_by_default(variant: Variant) -> None:
    """Logging every inbound request is opt-in; its default shows the toggle
    on the main configuration page, switched off."""
    config = _config(variant)
    assert config["options"]["debug_logging"] is False
    assert config["schema"]["debug_logging"] == "bool?"


def test_every_option_has_a_label_and_help_text(variant: Variant) -> None:
    """The Supervisor shows the translation's name and description; without
    one the user sees a bare option key."""
    translations = yaml.safe_load(
        (variant.addon_dir / "translations" / "en.yaml").read_text()
    )["configuration"]
    for key in _config(variant)["schema"]:
        assert translations[key].get("name"), key
        assert translations[key].get("description"), key


def test_app_and_component_versions_match(variant: Variant) -> None:
    """``start.py`` reinstalls the component and asks for a Home Assistant
    restart when the deployed manifest's version differs from the bundled
    one; a mismatch would mis-detect updates."""
    manifest = json.loads((variant.component_dir / "manifest.json").read_text())
    assert _config(variant)["version"] == manifest["version"]
