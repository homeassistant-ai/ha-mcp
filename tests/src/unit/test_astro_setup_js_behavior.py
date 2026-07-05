"""Behavioural tests for ``site/src/pages/setup.astro``'s wizard script.

The setup wizard's correctness is a multidimensional grid (5 server
methods x 19 clients x 2 scopes x 4 platforms x 4 remote paths). The
script is one giant state machine driving per-client instruction
templates; a typo or condition inversion silently breaks setup for one
or more branches and the only signal today is a user complaint.

Tests in this module:

* Pin the state-machine progression for the reworked server-method-first
  flow: method -> client -> scope -> (platform) -> (remote-path) ->
  config, including the branches where scope is skipped (stdio-local),
  where config is gated behind a remote-path choice (remote scope), and
  where an HTTP method plus a stdio-only client forces the platform step
  for the mcp-proxy bridge.
* Loop over every real client id and drive the happy path to config
  generation, asserting both that the per-client branch emitted into
  ``config-output`` AND that the emitted content is non-empty (so a typo
  that drops the JSON / CLI / instruction block is caught — not just "no
  JS error", which fires the moment any badge renders).

The harness rebuilds Astro's ``<script define:vars={...}>`` injection
by reading the real arrays out of the page frontmatter, so changes to
the production data show up in the test run immediately — no parallel
fixture to drift.
"""

from __future__ import annotations

import json
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

# Names the wizard's ``<script define:vars={...}>`` pulls in. The rework
# replaced the old connection/deployment model with a server-method +
# scope model, dropping ``connectionsData`` / ``deploymentData`` /
# ``httpOnlyClients`` entirely.
WIZARD_VAR_NAMES = [
    "clientsData",
    "platformsData",
    "stdioOnlyClients",
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


def _click(attr: str, val: str) -> str:
    """A single JSDOM click on the tile carrying ``data-<attr>="<val>"``."""
    return f"document.querySelector('[data-{attr}=\"{val}\"]').click();\n"


def _build_wizard_dom(wizard_vars: dict[str, Any]) -> str:
    """Build the minimum DOM the wizard script touches.

    Includes every section the script toggles, every selected-* badge
    and step-number element it writes to, every tile ``data-*`` attribute
    it listens for clicks on (one per hardcoded id or per array entry),
    plus the config-output structure ``generateConfig`` writes into.
    Client / platform tiles are built from the real var data so a new
    client / platform gets a tile automatically; server-method, scope,
    and remote-path ids are hardcoded in the markup so they are hardcoded
    here too.
    """
    # Sections ``updateSections`` toggles. ``section-method`` (step 1) is
    # statically visible in the real markup and never toggled, so it is
    # emitted separately without the ``hidden`` class.
    toggled_section_ids = [
        "section-client",
        "section-scope",
        "section-architecture",
        "section-platform",
        "section-remote-path",
        "section-config",
    ]
    arch_ids = ["arch-local", "arch-network", "arch-remote"]
    badge_ids = [
        "selected-method",
        "selected-client",
        "selected-platform-prev",
        "selected-remotepath-prev",
        "selected-final",
    ]

    parts = ["<!DOCTYPE html><html><body>"]
    # Step 1 is always visible; the script only ever scrolls to it.
    parts.append('<section id="section-method"></section>')
    parts.extend(
        f'<div id="{sid}" class="hidden"></div>' for sid in toggled_section_ids
    )
    parts.extend(f'<div id="{aid}" class="hidden"></div>' for aid in arch_ids)
    parts.extend(f'<span id="{bid}"></span>' for bid in badge_ids)
    parts.extend(
        [
            '<span id="platform-step-num"></span>',
            '<span id="remotepath-step-num"></span>',
            '<span id="config-step-num"></span>',
            '<p id="platform-help"></p>',
            # `start-over` is the one init-time getElementById the script
            # does NOT null-guard — omitting it aborts the whole wizard.
            '<button id="start-over"></button>',
            '<div id="config-summary"></div>',
            '<div id="setup-instructions"></div>',
            # `section-config` (the whole section, toggled by
            # updateSections) and `config-section` (the inner code block,
            # toggled by generateConfig for UI-format clients) — similar
            # names, distinct elements in the real page.
            '<div id="config-section"></div>',
            '<pre id="config-output"><code></code></pre>',
            '<div id="replace-hints"></div>',
            '<div id="config-notes"></div>',
        ]
    )

    # Server-method tiles — ids hardcoded in the markup.
    parts.extend(
        f'<button data-server-method="{mid}">{mid}</button>'
        for mid in ("ha-component", "ha-addon", "docker", "uvx", "stdio-local")
    )
    # Client tiles also carry `data-transports` (a JSON array) to mirror
    # the real markup. The reworked script reads transports from
    # `clientsData` (injected via define:vars) rather than this attr, but
    # keeping it matches production and guards a future handler that
    # parses it.
    parts.extend(
        "<button data-client=\"{cid}\" data-transports='{transports}'>{name}</button>".format(
            cid=c["id"],
            name=c["name"],
            transports=json.dumps(c.get("transports", [])),
        )
        for c in wizard_vars["clientsData"]
    )
    # Scope tiles — ids hardcoded (local | remote). `.scope-option`
    # carries the hover-preview listeners in the real markup.
    parts.extend(
        f'<button data-scope="{sid}" class="scope-option">{sid}</button>'
        for sid in ("local", "remote")
    )
    parts.extend(
        f'<button data-platform="{p["id"]}">{p["id"]}</button>'
        for p in wizard_vars["platformsData"]
    )
    # Remote-path tiles — ids hardcoded in the markup.
    parts.extend(
        f'<button data-remote-path="{pid}">{pid}</button>'
        for pid in ("builtin-webhook", "webhook-proxy", "cloudflared", "custom")
    )

    parts.append("</body></html>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# State-machine shape
# ---------------------------------------------------------------------------


class TestWizardStateMachine:
    """Each server method / scope combination unlocks a different section
    sequence; the tests below pin the visibility shape after each step.
    """

    def test_initial_state_downstream_sections_hidden(
        self, setup_script: str, prelude: str, wizard_vars: dict[str, Any]
    ) -> None:
        """Before any click, every downstream section
        (client / scope / architecture / platform / remote-path / config)
        stays hidden — only step 1 (section-method) is visible at load. A
        regression that toggled visibility at script init would skip the
        staged-flow UX entirely.
        """
        result = run_script(
            setup_script,
            prelude=prelude,
            initial_html=_build_wizard_dom(wizard_vars),
        )
        _assert_clean_init(result)
        for sid in (
            "section-client",
            "section-scope",
            "section-architecture",
            "section-platform",
            "section-remote-path",
            "section-config",
        ):
            assert _section_has_hidden_class(result.dom, sid), (
                f"#{sid} should still be hidden before any click"
            )

    def test_component_local_flow_reaches_config_without_platform(
        self, setup_script: str, prelude: str, wizard_vars: dict[str, Any]
    ) -> None:
        """ha-component -> cursor -> local: config unhides and the platform
        step never appears (the in-HA component needs no OS-specific
        commands)."""
        result = run_script(
            setup_script,
            prelude=prelude,
            initial_html=_build_wizard_dom(wizard_vars),
            invoke=(
                _click("server-method", "ha-component")
                + _click("client", "cursor")
                + _click("scope", "local")
            ),
        )
        _assert_clean_init(result)
        assert not _section_has_hidden_class(result.dom, "section-config"), (
            "section-config should be visible after ha-component + local scope"
        )
        assert _section_has_hidden_class(result.dom, "section-platform"), (
            "platform step should stay hidden for the ha-component local flow"
        )

    def test_stdio_flow_skips_scope_and_reaches_config(
        self, setup_script: str, prelude: str, wizard_vars: dict[str, Any]
    ) -> None:
        """stdio-local forces scope=local and has no scope step:
        stdio-local -> claude-desktop -> macos reaches config while
        section-scope stays hidden the whole time."""
        result = run_script(
            setup_script,
            prelude=prelude,
            initial_html=_build_wizard_dom(wizard_vars),
            invoke=(
                _click("server-method", "stdio-local")
                + _click("client", "claude-desktop")
                + _click("platform", "macos")
            ),
        )
        _assert_clean_init(result)
        assert _section_has_hidden_class(result.dom, "section-scope"), (
            "section-scope must stay hidden for the stdio-local flow "
            "(scope is forced to 'local')"
        )
        assert not _section_has_hidden_class(result.dom, "section-config"), (
            "section-config should be visible after stdio-local + platform"
        )

    def test_remote_flow_requires_remote_path_before_config(
        self, setup_script: str, prelude: str, wizard_vars: dict[str, Any]
    ) -> None:
        """Remote scope gates config behind a remote-path choice.

        ha-component -> claude-ai -> remote leaves ``shouldShowConfig()``
        false until a remote path is chosen — the safety net that prevents
        the wizard from offering instructions that omit the HTTPS
        front-end entirely. Records visibility before AND after the
        remote-path click as body data-* attrs so the assertion checks
        both transitions.
        """
        assert "claude-ai" in wizard_vars["remoteOnlyClients"], (
            "test premise: claude-ai must be a remote-only client"
        )
        result = run_script(
            setup_script,
            prelude=prelude,
            initial_html=_build_wizard_dom(wizard_vars),
            invoke=(
                _click("server-method", "ha-component")
                + _click("client", "claude-ai")
                + _click("scope", "remote")
                + """
              document.body.dataset.beforeRemotePath = String(
                document.getElementById('section-config').classList.contains('hidden')
              );
            """
                + _click("remote-path", "builtin-webhook")
                + """
              document.body.dataset.afterRemotePath = String(
                document.getElementById('section-config').classList.contains('hidden')
              );
            """
            ),
        )
        _assert_clean_init(result)
        assert 'data-before-remote-path="true"' in result.dom, (
            "config section should still be hidden before a remote path is chosen"
        )
        assert 'data-after-remote-path="false"' in result.dom, (
            "config section should be visible after the remote path is chosen"
        )

    def test_http_method_stdio_only_client_requires_platform(
        self, setup_script: str, prelude: str, wizard_vars: dict[str, Any]
    ) -> None:
        """An HTTP server method plus a stdio-only client forces the
        platform step for the mcp-proxy bridge.

        ha-addon -> claude-desktop (stdio-only) -> local leaves config
        hidden until a platform is chosen, because ``needsPlatform()``
        fires on ``isStdioOnly(client)`` for non-stdio-local methods.
        Records visibility before AND after the platform click.
        """
        assert "claude-desktop" in wizard_vars["stdioOnlyClients"], (
            "test premise: claude-desktop must be a stdio-only client"
        )
        result = run_script(
            setup_script,
            prelude=prelude,
            initial_html=_build_wizard_dom(wizard_vars),
            invoke=(
                _click("server-method", "ha-addon")
                + _click("client", "claude-desktop")
                + _click("scope", "local")
                + """
              document.body.dataset.beforePlatform = String(
                document.getElementById('section-config').classList.contains('hidden')
              );
            """
                + _click("platform", "macos")
                + """
              document.body.dataset.afterPlatform = String(
                document.getElementById('section-config').classList.contains('hidden')
              );
            """
            ),
        )
        _assert_clean_init(result)
        assert 'data-before-platform="true"' in result.dom, (
            "config section should still be hidden before the platform "
            "(mcp-proxy OS) is chosen"
        )
        assert 'data-after-platform="false"' in result.dom, (
            "config section should be visible after the platform is chosen"
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


def _flow_for_client(
    client_id: str, transports: list[str], remote_only: set[str]
) -> str:
    """Shortest click sequence that drives ``client_id`` to config.

    One flow per transport shape, each reaching ``generateConfig``:

    * remote-only (ChatGPT, Claude.ai): ha-component -> remote ->
      built-in webhook (they can't use local scope).
    * no stdio transport (Open WebUI): ha-addon -> local — an HTTP
      server serving an HTTP-only client, no platform step.
    * everything else (has stdio, whether or not it's stdio-only):
      stdio-local -> macOS platform — the simplest happy path.
    """
    if client_id in remote_only:
        return (
            _click("server-method", "ha-component")
            + _click("client", client_id)
            + _click("scope", "remote")
            + _click("remote-path", "builtin-webhook")
        )
    if "stdio" not in transports:
        return (
            _click("server-method", "ha-addon")
            + _click("client", client_id)
            + _click("scope", "local")
        )
    return (
        _click("server-method", "stdio-local")
        + _click("client", client_id)
        + _click("platform", "macos")
    )


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
        # Transports come from the real clientsData, not a hardcoded list,
        # so a client whose transports change gets the right flow next run.
        clients = {c["id"]: c for c in wizard_vars["clientsData"]}
        transports = clients[client_id].get("transports", [])
        remote_only = set(wizard_vars["remoteOnlyClients"])
        flow = _flow_for_client(client_id, transports, remote_only)

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
        # `_assert_clean_init` already covers init / transpile / invoke
        # / jsdom-channel errors. We deliberately don't fail on every
        # `result.errors` entry here — timer callback errors from
        # JSDOM-missing browser APIs (scrollIntoView, etc.) are noise,
        # not real regressions. The content-shape assertion below
        # catches the actual regression class (per-client branch silently
        # emitted nothing).

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
