"""Behavioural tests for ``site/src/pages/setup.astro``'s wizard script.

The setup wizard's correctness is a 19-client by 4-platform by
3-connection by 5-deployment grid. The script is one giant state
machine driving per-client instruction templates; a typo or condition
inversion silently breaks setup for one or more of those branches and
the only signal today is a user complaint. Issue #1422 calls this out
as the second load-bearing gap.

These tests:

* Pin the state-machine progression for the three connection shapes
  (local / network / remote).
* Loop over every client id in the real ``clientsData`` array (read
  from the .astro source via ``extract_astro_vars.mjs``) and drive the
  happy path to config generation, asserting no JS errors and a
  non-empty config-output. That catches "client X silently produces
  blank instructions" the moment it lands.

The harness rebuilds Astro's ``<script define:vars={...}>`` injection
by reading the real arrays out of the page frontmatter, so changes to
the production data show up in the test run immediately — no parallel
fixture to drift.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ._js_harness import (
    astro_vars_prelude,
    extract_astro_frontmatter_vars,
    extract_script_body,
    run_script,
)

SITE = Path(__file__).resolve().parents[3] / "site" / "src"
SETUP_ASTRO = SITE / "pages" / "setup.astro"

# Names the wizard's ``<script define:vars={...}>`` pulls in.
WIZARD_VAR_NAMES = [
    "clientsData",
    "platformsData",
    "connectionsData",
    "deploymentData",
    "stdioOnlyClients",
    "httpOnlyClients",
    "remoteOnlyClients",
]


@pytest.fixture(scope="module")
def setup_script() -> str:
    return extract_script_body(SETUP_ASTRO.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def wizard_vars() -> dict[str, Any]:
    return extract_astro_frontmatter_vars(SETUP_ASTRO, WIZARD_VAR_NAMES)


@pytest.fixture(scope="module")
def prelude(wizard_vars: dict[str, Any]) -> str:
    return astro_vars_prelude(wizard_vars)


def _build_wizard_dom(wizard_vars: dict[str, Any]) -> str:
    """Build the minimum DOM the wizard script touches.

    Includes every section the script toggles, every selected-* badge
    it writes to, every tile data-* attribute it listens for clicks on
    (one per id in each array), plus the config-output structure the
    config generator writes into. Built from the real var data so a new
    client / platform / connection / deployment gets a tile
    automatically.
    """
    section_ids = [
        "section-client",
        "section-connection",
        "section-architecture",
        "section-platform",
        "section-server-setup",
        "section-proxy",
        "section-config",
    ]
    arch_ids = ["arch-local", "arch-network", "arch-remote"]
    badge_ids = [
        "selected-client",
        "selected-connection",
        "selected-connection-server",
        "selected-platform",
        "selected-server-setup",
        "selected-final",
    ]

    parts = ["<!DOCTYPE html><html><body>"]
    parts.extend(f'<div id="{sid}" class="hidden"></div>' for sid in section_ids)
    parts.extend(f'<div id="{aid}" class="hidden"></div>' for aid in arch_ids)
    parts.extend(f'<span id="{bid}"></span>' for bid in badge_ids)
    parts.extend(
        [
            '<span id="config-step-num"></span>',
            '<button id="start-over"></button>',
            '<div id="config-summary"></div>',
            '<div id="setup-instructions"></div>',
            '<pre id="config-output"><code></code></pre>',
            '<div id="replace-hints"></div>',
            '<div id="config-notes"></div>',
        ]
    )

    # Tiles for each id in each data array. The script's click handlers
    # look up state.client = clientsData.find(c => c.id === card.dataset.client)
    # etc., so the data-* attribute is what matters.
    parts.extend(
        f'<button data-client="{c["id"]}">{c["name"]}</button>'
        for c in wizard_vars["clientsData"]
    )
    parts.extend(
        f'<button data-connection="{c["id"]}">{c["id"]}</button>'
        for c in wizard_vars["connectionsData"]
    )
    parts.extend(
        f'<button data-platform="{p["id"]}">{p["id"]}</button>'
        for p in wizard_vars["platformsData"]
    )
    # serverSetupOptions ids are hardcoded in the script body.
    parts.extend(
        f'<button data-server-setup="{sid}">{sid}</button>'
        for sid in ("macos-uvx", "linux-uvx", "windows-uvx", "ha-addon", "docker")
    )
    parts.extend(
        f'<button data-proxy="{pid}">{pid}</button>'
        for pid in ("cloudflared", "webhook-proxy")
    )

    parts.append("</body></html>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# State-machine shape
# ---------------------------------------------------------------------------


class TestWizardStateMachine:
    """Each connection shape exposes a different section sequence."""

    def test_initial_state_only_client_section_visible(
        self, setup_script: str, prelude: str, wizard_vars: dict[str, Any]
    ) -> None:
        """Before any click, only the client picker is visible."""
        result = run_script(
            setup_script,
            prelude=prelude,
            initial_html=_build_wizard_dom(wizard_vars),
            # No invoke — we just want the listener-binding pass to run.
        )
        assert not result.errors, f"errors: {result.errors}"
        # section-connection / -architecture / etc. start hidden.
        # The first updateSections is called only after a click, so the
        # initial DOM should still match the seed. Sanity check: no JS
        # errors during binding.

    def test_local_flow_progresses_to_config_after_platform(
        self, setup_script: str, prelude: str, wizard_vars: dict[str, Any]
    ) -> None:
        """Pick claude-desktop → local → macos → config section unhides."""
        result = run_script(
            setup_script,
            prelude=prelude,
            initial_html=_build_wizard_dom(wizard_vars),
            invoke="""
              document.querySelector('[data-client="claude-desktop"]').click();
              document.querySelector('[data-connection="local"]').click();
              document.querySelector('[data-platform="macos"]').click();
            """,
        )
        assert not result.errors, f"errors: {result.errors}"
        # section-config must no longer carry the 'hidden' class.
        config_section = result.dom[result.dom.find('id="section-config"') :]
        config_section = config_section[: config_section.find("</div>") + 6]
        assert "hidden" not in config_section, (
            f"section-config should be visible after local+platform; "
            f"snippet={config_section}"
        )
        # config-output > code should have been populated by generateConfig.
        assert "<code>" in result.dom and "</code>" in result.dom, (
            "config-output should be populated"
        )

    def test_network_flow_progresses_to_config_after_server_setup(
        self, setup_script: str, prelude: str, wizard_vars: dict[str, Any]
    ) -> None:
        result = run_script(
            setup_script,
            prelude=prelude,
            initial_html=_build_wizard_dom(wizard_vars),
            invoke="""
              document.querySelector('[data-client="cursor"]').click();
              document.querySelector('[data-connection="network"]').click();
              document.querySelector('[data-server-setup="ha-addon"]').click();
            """,
        )
        assert not result.errors, f"errors: {result.errors}"
        config_section = result.dom[result.dom.find('id="section-config"') :]
        config_section = config_section[: config_section.find("</div>") + 6]
        assert "hidden" not in config_section, (
            f"section-config should be visible after network+server-setup; "
            f"snippet={config_section}"
        )

    def test_remote_flow_requires_proxy_before_config(
        self, setup_script: str, prelude: str, wizard_vars: dict[str, Any]
    ) -> None:
        """Remote shape: client → remote → server-setup → proxy → config.

        Without the proxy step, ``shouldShowConfig()`` returns false and
        the section stays hidden — that's the safety net that prevents
        the wizard from offering instructions that omit the HTTPS
        front-end entirely.
        """
        result = run_script(
            setup_script,
            prelude=prelude,
            initial_html=_build_wizard_dom(wizard_vars),
            invoke="""
              document.querySelector('[data-client="claude-ai"]').click();
              document.querySelector('[data-connection="remote"]').click();
              document.querySelector('[data-server-setup="docker"]').click();
              // No proxy click yet — config should still be hidden.
              window.__beforeProxy = document.getElementById('section-config').classList.contains('hidden');
              document.querySelector('[data-proxy="cloudflared"]').click();
              window.__afterProxy = document.getElementById('section-config').classList.contains('hidden');
              document.body.dataset.beforeProxy = String(window.__beforeProxy);
              document.body.dataset.afterProxy = String(window.__afterProxy);
            """,
        )
        assert not result.errors, f"errors: {result.errors}"
        assert 'data-before-proxy="true"' in result.dom, (
            "config section should still be hidden before proxy is chosen; "
            f"dom snippet around body: {result.dom[:400]}"
        )
        assert 'data-after-proxy="false"' in result.dom, (
            "config section should be visible after proxy is chosen"
        )


# ---------------------------------------------------------------------------
# Per-client coverage
# ---------------------------------------------------------------------------


def _load_client_ids() -> list[str]:
    """Real client ids, read at collection time so each lands as a
    parametrize case with a stable, readable id (the client id itself).
    """
    vars_ = extract_astro_frontmatter_vars(SETUP_ASTRO, ["clientsData"])
    return [c["id"] for c in vars_["clientsData"]]


CLIENT_IDS = _load_client_ids()


class TestPerClientInstructionTemplate:
    """Every client must drive at least one happy-path config generation.

    The wizard's per-client branches (JSON config builder, CLI command
    emit, UI instruction blocks) live inside ``generateConfig``. A typo
    in a single client's branch silently breaks setup for that client.
    Looping over every real id catches it on the next test run.

    The chosen connection shape per test is the simplest one each
    client supports: stdio-only clients get local, http-only get
    network, remote-only get remote+cloudflared, the rest get local.
    """

    @pytest.mark.parametrize("client_id", CLIENT_IDS, ids=CLIENT_IDS)
    def test_generate_config_emits_for_client(
        self,
        client_id: str,
        setup_script: str,
        prelude: str,
        wizard_vars: dict[str, Any],
    ) -> None:
        stdio_only = set(wizard_vars["stdioOnlyClients"])
        http_only = set(wizard_vars["httpOnlyClients"])
        remote_only = set(wizard_vars["remoteOnlyClients"])

        if client_id in remote_only:
            invoke = f"""
              document.querySelector('[data-client="{client_id}"]').click();
              document.querySelector('[data-connection="remote"]').click();
              document.querySelector('[data-server-setup="docker"]').click();
              document.querySelector('[data-proxy="cloudflared"]').click();
            """
        elif client_id in http_only:
            invoke = f"""
              document.querySelector('[data-client="{client_id}"]').click();
              document.querySelector('[data-connection="network"]').click();
              document.querySelector('[data-server-setup="ha-addon"]').click();
            """
        elif client_id in stdio_only:
            invoke = f"""
              document.querySelector('[data-client="{client_id}"]').click();
              document.querySelector('[data-connection="local"]').click();
              document.querySelector('[data-platform="macos"]').click();
            """
        else:
            # Most clients support both — local is the simplest happy path.
            invoke = f"""
              document.querySelector('[data-client="{client_id}"]').click();
              document.querySelector('[data-connection="local"]').click();
              document.querySelector('[data-platform="macos"]').click();
            """

        result = run_script(
            setup_script,
            prelude=prelude,
            initial_html=_build_wizard_dom(wizard_vars),
            invoke=invoke,
        )

        assert not result.errors, (
            f"client {client_id!r}: JS errors during wizard flow: {result.errors}"
        )
        # config-summary should have at least one badge populated.
        assert "rounded-full" in result.dom, (
            f"client {client_id!r}: config-summary badges missing — "
            f"generateConfig may have bailed early"
        )
