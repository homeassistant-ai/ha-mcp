"""Platform-specific content of the setup wizard's stdio and tunnel output.

The generic per-client test in ``test_astro_setup_js_behavior.py`` only checks
that a branch emitted something. These pin what the Docker and Windows
platform choices change, where a stale template silently hands the user a
command that cannot work.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from ._astro_setup_wizard import (
    _assert_clean_init,
    _build_wizard_dom,
    _click,
    bundle_setup_script,
    load_wizard_vars,
)
from ._js_harness import run_script

_CAPTURE = """
document.body.dataset.configCode = (
  document.querySelector('#config-output code').textContent || ''
);
document.body.dataset.instructionsHtml = String(
  document.getElementById('setup-instructions').innerHTML || ''
);
"""


@pytest.fixture(scope="module")
def setup_script() -> str:
    return bundle_setup_script()


@pytest.fixture(scope="module")
def wizard_vars() -> dict[str, Any]:
    return load_wizard_vars()


def _stdio_output(
    setup_script: str, wizard_vars: dict[str, Any], client_id: str, platform: str
) -> tuple[str, str]:
    """``(config, instructions)`` after stdio-local -> client -> platform."""
    result = run_script(
        setup_script,
        initial_html=_build_wizard_dom(wizard_vars),
        invoke=(
            _click("server-method", "stdio-local")
            + _click("client", client_id)
            + _click("platform", platform)
            + _CAPTURE
        ),
    )
    _assert_clean_init(result)
    config = re.search(r'data-config-code="([^"]*)"', result.dom)
    instructions = re.search(r'data-instructions-html="([^"]*)"', result.dom)
    assert config is not None and instructions is not None, "output not captured"
    # Attribute round-trip: JSDOM serialises `"` as &quot; and newlines as &#10;.
    return (
        config.group(1).replace("&quot;", '"').replace("&#10;", "\n"),
        instructions.group(1),
    )


@pytest.mark.parametrize("client_id", ["codex", "gemini-cli", "claude-code"])
def test_docker_stdio_command_passes_credentials_into_the_container(
    client_id: str, setup_script: str, wizard_vars: dict[str, Any]
) -> None:
    """A ``docker run`` without ``-e`` starts a server that cannot reach Home
    Assistant: the image defaults both variables to empty strings, and an env
    set on the client side stays with the docker CLI process."""
    config, _ = _stdio_output(setup_script, wizard_vars, client_id, "docker")

    # Gemini CLI spells it `docker -- run ...`, so match the run flags, not "docker run".
    assert "run --rm -i" in config, config
    assert "ghcr.io/homeassistant-ai/ha-mcp:latest" in config, config
    assert "-e HOMEASSISTANT_URL={{HOMEASSISTANT_URL}}" in config, config
    assert "-e HOMEASSISTANT_TOKEN={{HOMEASSISTANT_TOKEN}}" in config, config


def test_copilot_cli_docker_stdio_steps_use_the_docker_command(
    setup_script: str, wizard_vars: dict[str, Any]
) -> None:
    """The Copilot CLI form steps are the config for that client, so with the
    Docker platform they must name the container command, not ``uvx``."""
    _, instructions = _stdio_output(setup_script, wizard_vars, "copilot-cli", "docker")

    assert "docker run --rm -i" in instructions, instructions[:600]
    assert "uvx ha-mcp@latest" not in instructions, instructions[:600]


def test_docker_stdio_settings_hint_does_not_point_at_host_files(
    setup_script: str, wizard_vars: dict[str, Any]
) -> None:
    """The stdio sidecar binds 127.0.0.1 inside the container and writes
    ``ui.url`` into the volume, so the host-file instructions do not apply."""
    _, instructions = _stdio_output(setup_script, wizard_vars, "claude-code", "docker")

    assert "ui.url" not in instructions, instructions[-800:]
    assert "ha-mcp-data" in instructions, instructions[-800:]


@pytest.mark.parametrize("client_id", ["gemini-cli", "claude-code"])
@pytest.mark.parametrize(
    ("platform", "multiline"),
    [("macos", True), ("windows", False)],
    ids=["macos", "windows"],
)
def test_cli_commands_use_line_continuations_only_where_the_shell_has_them(
    client_id: str,
    platform: str,
    multiline: bool,
    setup_script: str,
    wizard_vars: dict[str, Any],
) -> None:
    """PowerShell treats a trailing backslash as text, so the Windows command
    is one line; POSIX shells keep the readable continuation form."""
    config, _ = _stdio_output(setup_script, wizard_vars, client_id, platform)

    assert ("\\\n" in config) is multiline, config
    assert "ha-mcp@latest" in config, config


def test_continue_http_config_is_a_standalone_block(
    setup_script: str, wizard_vars: dict[str, Any]
) -> None:
    """Continue's ``.continue/mcpServers/*.yaml`` files need the document
    metadata (``name``, ``version``, ``schema``); the stdio variant has it and
    the HTTP variant must too."""
    result = run_script(
        setup_script,
        initial_html=_build_wizard_dom(wizard_vars),
        invoke=(
            _click("server-method", "ha-addon")
            + _click("client", "continue")
            + _click("scope", "local")
            + _CAPTURE
        ),
    )
    _assert_clean_init(result)
    match = re.search(r'data-config-code="([^"]*)"', result.dom)
    assert match is not None, "config-output was not captured"
    config = match.group(1).replace("&quot;", '"').replace("&#10;", "\n")

    # name and version are free-form (so not pinned); schema v1 is Continue's.
    assert re.match(r"name: \S.*\nversion: \S+\nschema: v1\n", config), config


def test_scope_hover_leaving_restores_the_selected_diagram(
    setup_script: str, wizard_vars: dict[str, Any]
) -> None:
    """Hovering the other scope tile previews its diagram; moving away must
    bring the selected scope's diagram back instead of leaving the preview."""
    result = run_script(
        setup_script,
        initial_html=_build_wizard_dom(wizard_vars),
        invoke=(
            _click("server-method", "ha-addon")
            + _click("client", "cursor")
            + _click("scope", "local")
            + """
            const remote = document.querySelector('[data-scope="remote"]');
            remote.dispatchEvent(new window.MouseEvent('mouseenter'));
            document.body.dataset.hoverRemoteHidden = String(
              document.getElementById('arch-remote').classList.contains('hidden')
            );
            remote.dispatchEvent(new window.MouseEvent('mouseleave'));
            document.body.dataset.afterNetworkHidden = String(
              document.getElementById('arch-network').classList.contains('hidden')
            );
            document.body.dataset.afterRemoteHidden = String(
              document.getElementById('arch-remote').classList.contains('hidden')
            );
            """
        ),
    )
    _assert_clean_init(result)
    assert 'data-hover-remote-hidden="false"' in result.dom, (
        "hover should preview remote"
    )
    assert 'data-after-network-hidden="false"' in result.dom, (
        "selected diagram not restored"
    )
    assert 'data-after-remote-hidden="true"' in result.dom, (
        "preview diagram still shown"
    )
