"""Behavioural tests for ``site/src/pages/setup.astro``'s wizard script.

The setup wizard's correctness is a multidimensional grid (19 clients x
4 platforms x 3 connections x 5 deployments). The script is one giant
state machine driving per-client instruction templates; a typo or
condition inversion silently breaks setup for one or more branches and
the only signal today is a user complaint.

Tests in this module:

* Pin the state-machine progression for the three connection shapes
  (local / network / remote).
* Loop over every real client id and drive the happy path to config
  generation, asserting both that the per-client branch emitted into
  ``config-output`` AND that the emitted content names the client (so
  a typo that drops the JSON / CLI / instruction block is caught — not
  just "no JS error", which fires the moment any badge renders).

The harness rebuilds Astro's ``<script define:vars={...}>`` injection
by reading the real arrays out of the page frontmatter, so changes to
the production data show up in the test run immediately — no parallel
fixture to drift.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from ._js_harness import (
    HarnessResult,
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
    return extract_script_body(
        SETUP_ASTRO.read_text(encoding="utf-8"),
        source_label=str(SETUP_ASTRO),
    )


@pytest.fixture(scope="module")
def wizard_vars() -> dict[str, Any]:
    return extract_astro_frontmatter_vars(SETUP_ASTRO, WIZARD_VAR_NAMES)


@pytest.fixture(scope="module")
def prelude(wizard_vars: dict[str, Any]) -> str:
    return astro_vars_prelude(wizard_vars)


def _assert_clean_init(result: HarnessResult) -> None:
    """Fail loud on script-init / transpile / jsdom errors so a
    regression that breaks init isn't reported as "feature didn't fire"."""
    init_errors = [
        e
        for e in result.errors
        if e.startswith(("script init:", "transpile failure", "invoke:", "jsdom error"))
    ]
    assert not init_errors, f"script failed to initialise: {init_errors}"


def _section_has_hidden_class(dom: str, section_id: str) -> bool:
    """True iff the element with the given id has ``hidden`` in its
    class attribute. More robust than a substring window — finds the
    exact element and inspects its class attribute, tolerating
    attribute-order changes and additional classes.
    """
    match = re.search(
        rf'<[^>]*\bid="{re.escape(section_id)}"[^>]*\bclass="([^"]*)"',
        dom,
    )
    if match is None:
        # Class attr might come before id; try the other ordering.
        match = re.search(
            rf'<[^>]*\bclass="([^"]*)"[^>]*\bid="{re.escape(section_id)}"',
            dom,
        )
    if match is None:
        raise AssertionError(f"#{section_id} not found in dom")
    return "hidden" in match.group(1).split()


def _build_wizard_dom(wizard_vars: dict[str, Any]) -> str:
    """Build the minimum DOM the wizard script touches.

    Includes every section the script toggles, every selected-* badge
    it writes to, every tile data-* attribute it listens for clicks on
    (one per id in each array), plus the config-output structure
    ``generateConfig`` writes into. Built from the real var data so a
    new client / platform / connection / deployment gets a tile
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
            # `section-config` (the whole section, toggled by
            # updateSections) and `config-section` (the inner code
            # block, toggled by generateConfig for UI-format clients) —
            # similar names, distinct elements in the real page.
            '<div id="config-section"></div>',
            '<pre id="config-output"><code></code></pre>',
            '<div id="replace-hints"></div>',
            '<div id="config-notes"></div>',
        ]
    )

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
        """Before any click, every downstream section
        (connection / architecture / platform / server-setup / proxy /
        config) stays hidden. A regression that toggled visibility at
        script init would skip the staged-flow UX entirely.
        """
        result = run_script(
            setup_script,
            prelude=prelude,
            initial_html=_build_wizard_dom(wizard_vars),
        )
        _assert_clean_init(result)
        for sid in (
            "section-connection",
            "section-architecture",
            "section-platform",
            "section-server-setup",
            "section-proxy",
            "section-config",
        ):
            assert _section_has_hidden_class(result.dom, sid), (
                f"#{sid} should still be hidden before any click"
            )

    def test_local_flow_progresses_to_config_after_platform(
        self, setup_script: str, prelude: str, wizard_vars: dict[str, Any]
    ) -> None:
        """Pick claude-desktop -> local -> macos -> config section unhides."""
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
        _assert_clean_init(result)
        assert not _section_has_hidden_class(result.dom, "section-config"), (
            "section-config should be visible after local+platform"
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
        _assert_clean_init(result)
        assert not _section_has_hidden_class(result.dom, "section-config"), (
            "section-config should be visible after network+server-setup"
        )

    def test_remote_flow_requires_proxy_before_config(
        self, setup_script: str, prelude: str, wizard_vars: dict[str, Any]
    ) -> None:
        """Remote shape: client -> remote -> server-setup -> proxy -> config.

        Without the proxy step, ``shouldShowConfig()`` returns false
        and the section stays hidden — the safety net that prevents
        the wizard from offering instructions that omit the HTTPS
        front-end entirely. Records visibility before AND after the
        proxy click as body data-* attrs so the assertion checks both
        transitions.
        """
        result = run_script(
            setup_script,
            prelude=prelude,
            initial_html=_build_wizard_dom(wizard_vars),
            invoke="""
              document.querySelector('[data-client="claude-ai"]').click();
              document.querySelector('[data-connection="remote"]').click();
              document.querySelector('[data-server-setup="docker"]').click();
              document.body.dataset.beforeProxy = String(
                document.getElementById('section-config').classList.contains('hidden')
              );
              document.querySelector('[data-proxy="cloudflared"]').click();
              document.body.dataset.afterProxy = String(
                document.getElementById('section-config').classList.contains('hidden')
              );
            """,
        )
        _assert_clean_init(result)
        assert 'data-before-proxy="true"' in result.dom, (
            "config section should still be hidden before proxy is chosen"
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

    Each test captures the post-flow ``config-output`` text plus the
    rendered instructions HTML into body dataset attrs so the assertion
    can verify the per-client branch actually emitted content — pinning
    that the branch ran, not just that some upstream code rendered a
    badge.
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
            flow = (
                f"document.querySelector('[data-client=\"{client_id}\"]').click();\n"
                "document.querySelector('[data-connection=\"remote\"]').click();\n"
                "document.querySelector('[data-server-setup=\"docker\"]').click();\n"
                "document.querySelector('[data-proxy=\"cloudflared\"]').click();\n"
            )
        elif client_id in http_only:
            flow = (
                f"document.querySelector('[data-client=\"{client_id}\"]').click();\n"
                "document.querySelector('[data-connection=\"network\"]').click();\n"
                "document.querySelector('[data-server-setup=\"ha-addon\"]').click();\n"
            )
        elif client_id in stdio_only:
            flow = (
                f"document.querySelector('[data-client=\"{client_id}\"]').click();\n"
                "document.querySelector('[data-connection=\"local\"]').click();\n"
                "document.querySelector('[data-platform=\"macos\"]').click();\n"
            )
        else:
            flow = (
                f"document.querySelector('[data-client=\"{client_id}\"]').click();\n"
                "document.querySelector('[data-connection=\"local\"]').click();\n"
                "document.querySelector('[data-platform=\"macos\"]').click();\n"
            )

        # After the flow, snapshot the emitted content into body dataset
        # attrs the assertion can read. Two captures: the <code> body
        # (the JSON / CLI primary output) and the rendered instructions
        # block (the UI-only path that doesn't write to <code>).
        invoke = (
            flow
            + """
            const codeEl = document.querySelector('#config-output code');
            const instructionsEl = document.getElementById('setup-instructions');
            document.body.dataset.configCode = (codeEl && codeEl.textContent) || '';
            document.body.dataset.configInstructionsLen = String(
              (instructionsEl && instructionsEl.innerHTML || '').length
            );
            """
        )

        result = run_script(
            setup_script,
            prelude=prelude,
            initial_html=_build_wizard_dom(wizard_vars),
            invoke=invoke,
        )
        _assert_clean_init(result)
        assert not result.errors, (
            f"client {client_id!r}: errors during wizard flow: {result.errors}"
        )

        # Every per-client branch must emit SOMETHING — either populated
        # config code, or non-empty instructions HTML. A typo that drops
        # the entire branch leaves both empty.
        code_match = re.search(r'data-config-code="([^"]*)"', result.dom)
        instructions_len_match = re.search(
            r'data-config-instructions-len="(\d+)"', result.dom
        )
        code_body = (code_match.group(1) if code_match else "").strip()
        instructions_len = (
            int(instructions_len_match.group(1)) if instructions_len_match else 0
        )

        assert code_body or instructions_len > 0, (
            f"client {client_id!r}: config-output AND setup-instructions both "
            f"empty after wizard flow — generateConfig's per-client branch "
            f"probably bailed; "
            f"config_code={code_body!r}, instructions_len={instructions_len}"
        )
