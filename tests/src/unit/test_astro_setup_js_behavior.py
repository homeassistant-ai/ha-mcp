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
    # `base` is injected via define:vars from import.meta.env.BASE_URL, which
    # frontmatter extraction cannot evaluate - define it empty here.
    return astro_vars_prelude(wizard_vars) + "\nconst base = '';"


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
    # carries the hover-preview listeners in the real markup. The remote
    # tile embeds a `.complexity-badge` child (as the real markup does) so
    # updateSections' badge relabel — Quick for ha-component, Advanced for
    # the tunnel/proxy methods — has a target element to write into.
    for sid in ("local", "remote"):
        if sid == "remote":
            parts.append(
                f'<button data-scope="{sid}" class="scope-option">{sid}'
                '<span class="complexity-badge" data-complexity="advanced">'
                "Advanced</span></button>"
            )
        else:
            parts.append(
                f'<button data-scope="{sid}" class="scope-option">{sid}</button>'
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


# ---------------------------------------------------------------------------
# Remote-path tile filtering (updateSections -> remotePathsForMethod)
# ---------------------------------------------------------------------------

# The remote-path tiles surfaced for a given server method, keyed by the tile's
# ``data-remote-path`` id and the camelCase body-dataset key each hidden-state
# capture writes into.
_REMOTE_PATH_TILES = {
    "builtin-webhook": "builtinHidden",
    "webhook-proxy": "proxyHidden",
    "cloudflared": "cloudflaredHidden",
    "custom": "customHidden",
}


def _remote_path_hidden_states(
    setup_script: str, prelude: str, wizard_vars: dict[str, Any], method: str
) -> dict[str, bool]:
    """Drive ``method -> cursor -> remote`` and return ``{remote-path id: hidden}``.

    Captures each remote-path tile's ``classList.contains('hidden')`` into a
    body-dataset attr inside ``invoke`` (the live-DOM pattern the other tests
    use) so the post-run serialized DOM carries the visibility decision.
    """
    capture = "".join(
        f"document.body.dataset.{camel} = String("
        f"document.querySelector('[data-remote-path=\"{pid}\"]')"
        f".classList.contains('hidden'));\n"
        for pid, camel in _REMOTE_PATH_TILES.items()
    )
    result = run_script(
        setup_script,
        prelude=prelude,
        initial_html=_build_wizard_dom(wizard_vars),
        invoke=(
            _click("server-method", method)
            + _click("client", "cursor")
            + _click("scope", "remote")
            + capture
        ),
    )
    _assert_clean_init(result)
    states: dict[str, bool] = {}
    for pid, camel in _REMOTE_PATH_TILES.items():
        attr = "data-" + re.sub(r"([A-Z])", r"-\1", camel).lower()
        match = re.search(rf'{attr}="(true|false)"', result.dom)
        assert match is not None, f"{attr} not captured for method {method!r}"
        states[pid] = match.group(1) == "true"
    return states


class TestRemotePathFiltering:
    """``updateSections`` shows only the remote-path tiles that apply to the
    chosen server method (``remotePathsForMethod``); the rest are hidden. A tile
    surfaced for the wrong method offers an instruction path that can't work.
    """

    def test_ha_component_shows_builtin_hides_proxy(
        self, setup_script: str, prelude: str, wizard_vars: dict[str, Any]
    ) -> None:
        # ha-component's own webhook is built in; the webhook-proxy app is not
        # its path, so that tile is hidden while builtin-webhook is shown.
        states = _remote_path_hidden_states(
            setup_script, prelude, wizard_vars, "ha-component"
        )
        assert states["builtin-webhook"] is False
        assert states["webhook-proxy"] is True
        assert states["cloudflared"] is False
        assert states["custom"] is False

    def test_ha_addon_shows_proxy_hides_builtin(
        self, setup_script: str, prelude: str, wizard_vars: dict[str, Any]
    ) -> None:
        # The add-on has no built-in HA webhook; the webhook-proxy app is its
        # first-class remote path, so the two swap visibility versus component.
        states = _remote_path_hidden_states(
            setup_script, prelude, wizard_vars, "ha-addon"
        )
        assert states["webhook-proxy"] is False
        assert states["builtin-webhook"] is True
        assert states["cloudflared"] is False
        assert states["custom"] is False

    def test_docker_hides_builtin_and_proxy(
        self, setup_script: str, prelude: str, wizard_vars: dict[str, Any]
    ) -> None:
        # A raw docker/uvx HTTP server has neither an HA webhook nor the proxy
        # app; only the generic tunnel/custom-proxy paths apply.
        states = _remote_path_hidden_states(
            setup_script, prelude, wizard_vars, "docker"
        )
        assert states["builtin-webhook"] is True
        assert states["webhook-proxy"] is True
        assert states["cloudflared"] is False
        assert states["custom"] is False


# ---------------------------------------------------------------------------
# Tile disable guards (incompatible method/client/scope combinations)
# ---------------------------------------------------------------------------


class TestTileDisableGuards:
    """Picking a method or client disables the tiles that can't work with it,
    so the user can't step into an unbuildable combination.
    """

    def test_stdio_local_disables_transportless_client(
        self, setup_script: str, prelude: str, wizard_vars: dict[str, Any]
    ) -> None:
        # ChatGPT has no stdio transport (sse/streamable-http only); the local
        # stdio server can't serve it, so its client tile is disabled.
        result = run_script(
            setup_script,
            prelude=prelude,
            initial_html=_build_wizard_dom(wizard_vars),
            invoke=(
                _click("server-method", "stdio-local")
                + "document.body.dataset.chatgptDisabled = String("
                "document.querySelector('[data-client=\"chatgpt\"]').disabled);\n"
            ),
        )
        _assert_clean_init(result)
        assert 'data-chatgpt-disabled="true"' in result.dom

    def test_remote_only_client_disables_local_scope(
        self, setup_script: str, prelude: str, wizard_vars: dict[str, Any]
    ) -> None:
        # Claude.ai is remote-only (requires HTTPS); once chosen, the local
        # scope tile is disabled so it can't be paired with a LAN-only setup.
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
                + "document.body.dataset.localScopeDisabled = String("
                "document.querySelector('[data-scope=\"local\"]').disabled);\n"
            ),
        )
        _assert_clean_init(result)
        assert 'data-local-scope-disabled="true"' in result.dom


# ---------------------------------------------------------------------------
# uvx platform gating (needsPlatform for the server host OS)
# ---------------------------------------------------------------------------


class TestUvxPlatformGating:
    """The uvx server method always needs the host OS, so config is gated behind
    the platform step even for a full-transport client on local scope.
    """

    def test_uvx_local_gates_config_behind_platform(
        self, setup_script: str, prelude: str, wizard_vars: dict[str, Any]
    ) -> None:
        # uvx -> cursor -> local leaves config hidden (needsPlatform() fires on
        # the uvx method) until an OS is chosen, then it unhides. Mirrors the
        # mcp-proxy gating test: records visibility before AND after the click.
        result = run_script(
            setup_script,
            prelude=prelude,
            initial_html=_build_wizard_dom(wizard_vars),
            invoke=(
                _click("server-method", "uvx")
                + _click("client", "cursor")
                + _click("scope", "local")
                + """
              document.body.dataset.beforePlatform = String(
                document.getElementById('section-config').classList.contains('hidden')
              );
            """
                + _click("platform", "linux")
                + """
              document.body.dataset.afterPlatform = String(
                document.getElementById('section-config').classList.contains('hidden')
              );
            """
            ),
        )
        _assert_clean_init(result)
        assert 'data-before-platform="true"' in result.dom, (
            "config section should still be hidden before the uvx host OS is chosen"
        )
        assert 'data-after-platform="false"' in result.dom, (
            "config section should be visible after the uvx host OS is chosen"
        )


# ---------------------------------------------------------------------------
# Start over (reset back to step 1)
# ---------------------------------------------------------------------------


class TestStartOver:
    def test_start_over_hides_downstream_sections(
        self, setup_script: str, prelude: str, wizard_vars: dict[str, Any]
    ) -> None:
        """A full flow followed by Start Over collapses the wizard back to step
        1: client / scope / config are all hidden again."""
        result = run_script(
            setup_script,
            prelude=prelude,
            initial_html=_build_wizard_dom(wizard_vars),
            invoke=(
                _click("server-method", "ha-component")
                + _click("client", "cursor")
                + _click("scope", "local")
                + "document.getElementById('start-over').click();\n"
            ),
        )
        _assert_clean_init(result)
        for sid in ("section-client", "section-scope", "section-config"):
            assert _section_has_hidden_class(result.dom, sid), (
                f"#{sid} should be hidden again after Start Over"
            )


# ---------------------------------------------------------------------------
# Remote-scope complexity badge relabel (updateSections)
# ---------------------------------------------------------------------------


class TestRemoteScopeBadge:
    def test_badge_is_quick_for_component_advanced_for_docker(
        self, setup_script: str, prelude: str, wizard_vars: dict[str, Any]
    ) -> None:
        """ha-component makes remote access the easy path (its built-in webhook),
        so the Remote scope badge reads 'Quick'; a tunnel/proxy method (docker)
        relabels it 'Advanced'."""
        result = run_script(
            setup_script,
            prelude=prelude,
            initial_html=_build_wizard_dom(wizard_vars),
            invoke=(
                _click("server-method", "ha-component")
                + "document.body.dataset.badgeAfterComponent = "
                'document.querySelector(\'[data-scope="remote"] '
                ".complexity-badge').textContent;\n"
                + _click("server-method", "docker")
                + "document.body.dataset.badgeAfterDocker = "
                'document.querySelector(\'[data-scope="remote"] '
                ".complexity-badge').textContent;\n"
            ),
        )
        _assert_clean_init(result)
        assert 'data-badge-after-component="Quick"' in result.dom
        assert 'data-badge-after-docker="Advanced"' in result.dom


# ---------------------------------------------------------------------------
# Tunnel port per server method (serverPort regression)
# ---------------------------------------------------------------------------


class TestTunnelPort:
    """The cloudflared/tunnel instructions must target the port that matches the
    server method — 9583 for the HA add-on, 8086 for a raw docker/uvx HTTP
    server — never the other one. Pinned in both directions; the docker case
    also click-drives the docker server-method branch.
    """

    @pytest.mark.parametrize(
        "method, expected_port, wrong_port",
        [
            ("ha-addon", "localhost:9583", "localhost:8086"),
            ("docker", "localhost:8086", "localhost:9583"),
        ],
        ids=["ha-addon", "docker"],
    )
    def test_cloudflared_targets_method_port(
        self,
        method: str,
        expected_port: str,
        wrong_port: str,
        setup_script: str,
        prelude: str,
        wizard_vars: dict[str, Any],
    ) -> None:
        result = run_script(
            setup_script,
            prelude=prelude,
            initial_html=_build_wizard_dom(wizard_vars),
            invoke=(
                _click("server-method", method)
                + _click("client", "cursor")
                + _click("scope", "remote")
                + _click("remote-path", "cloudflared")
                + "document.body.dataset.instructions = "
                "document.getElementById('setup-instructions').innerHTML;\n"
            ),
        )
        _assert_clean_init(result)
        match = re.search(r'data-instructions="([^"]*)"', result.dom)
        assert match is not None, "setup-instructions innerHTML was not captured"
        instructions = match.group(1)
        # `localhost:` prefix is deliberate: the cloudflared block also carries
        # hardcoded bare `...:9583` HAOS-app examples, so a bare port substring
        # would false-match. Only the `localhost:${serverPort}` lines vary.
        assert expected_port in instructions
        assert wrong_port not in instructions


# ---------------------------------------------------------------------------
# Legacy-OAuth gating (legacyOauthReady -> working steps vs. method notice)
# ---------------------------------------------------------------------------

# Verbatim markers from setup.astro's instruction templates. Plain text only:
# the capture reads `setup-instructions` innerHTML back out of a serialized
# data-* attribute, where markup comes back entity-escaped, so a substring
# containing tags or entities would never match.
_LEGACY_NOTICE_HEADLINE = (
    "Pick the HA-MCP Server component with its built-in webhook first"
)
_LEGACY_NOTICE_GO_BACK = "Go back to"
_SPARK_LEGACY_WHY = "Why legacy OAuth mode"
_COPILOT_HTTP_TITLE = "Copilot CLI Configuration (HTTP)"
_COPILOT_STDIO_TITLE = "Copilot CLI Configuration (STDIO)"
_COPILOT_CLIENT_ID_CALLOUT = "Client ID required: use legacy OAuth mode"


def _instructions_after(
    setup_script: str, prelude: str, wizard_vars: dict[str, Any], flow: str
) -> str:
    """Drive ``flow`` and return the rendered ``setup-instructions`` HTML.

    Captured into a body dataset attr inside ``invoke`` (the live-DOM pattern
    the other tests use); callers assert plain-text substrings only, per the
    escaping note above.
    """
    result = run_script(
        setup_script,
        prelude=prelude,
        initial_html=_build_wizard_dom(wizard_vars),
        invoke=(
            flow + "document.body.dataset.instructions = "
            "document.getElementById('setup-instructions').innerHTML;\n"
        ),
    )
    _assert_clean_init(result)
    match = re.search(r'data-instructions="([^"]*)"', result.dom)
    assert match is not None, "setup-instructions innerHTML was not captured"
    return match.group(1)


class TestLegacyOauthGating:
    """``legacyOauthReady`` (ha-component AND builtin-webhook remote path)
    decides whether the OAuth-only UI clients — Gemini Spark, Copilot CLI over
    HTTP — get their working legacy-mode steps or the amber "pick the
    component first" notice. A flipped gate hands users an instruction set
    whose OAuth flow cannot complete (or hides the working one), so both
    sides are pinned with branch-specific markers, not just non-emptiness.
    """

    def test_copilot_cli_http_on_builtin_webhook_gets_client_id_steps(
        self, setup_script: str, prelude: str, wizard_vars: dict[str, Any]
    ) -> None:
        """ha-component -> copilot-cli -> remote -> builtin-webhook is the
        legacy-eligible HTTP path: the HTTP form steps plus the legacy
        Client ID / Client Secret callout render, and neither the STDIO
        branch (copilot-cli has a stdio transport too) nor the notice fires.
        """
        instructions = _instructions_after(
            setup_script,
            prelude,
            wizard_vars,
            _click("server-method", "ha-component")
            + _click("client", "copilot-cli")
            + _click("scope", "remote")
            + _click("remote-path", "builtin-webhook"),
        )
        assert _COPILOT_HTTP_TITLE in instructions
        assert _COPILOT_CLIENT_ID_CALLOUT in instructions
        assert _COPILOT_STDIO_TITLE not in instructions
        assert _LEGACY_NOTICE_HEADLINE not in instructions

    def test_gemini_spark_on_builtin_webhook_gets_legacy_steps(
        self, setup_script: str, prelude: str, wizard_vars: dict[str, Any]
    ) -> None:
        """Same eligible path for Spark renders the legacy-mode walkthrough
        (pinned via its "Why legacy OAuth mode" callout — the notice shares
        the "Gemini Spark Configuration" title, so the title alone cannot
        discriminate) and not the notice."""
        instructions = _instructions_after(
            setup_script,
            prelude,
            wizard_vars,
            _click("server-method", "ha-component")
            + _click("client", "gemini-spark")
            + _click("scope", "remote")
            + _click("remote-path", "builtin-webhook"),
        )
        assert _SPARK_LEGACY_WHY in instructions
        assert _LEGACY_NOTICE_HEADLINE not in instructions

    # Two incompatible shapes, one per side of the `legacyOauthReady`
    # conjunction: right method but wrong remote path (component +
    # cloudflared), and wrong method entirely (docker). `working_marker` is
    # the branch-specific text that must NOT render alongside the notice.
    @pytest.mark.parametrize(
        "client_id, method, working_marker",
        [
            ("copilot-cli", "ha-component", _COPILOT_HTTP_TITLE),
            ("gemini-spark", "docker", _SPARK_LEGACY_WHY),
        ],
        ids=["copilot-cli-component-cloudflared", "gemini-spark-docker"],
    )
    def test_incompatible_path_shows_legacy_method_notice(
        self,
        client_id: str,
        method: str,
        working_marker: str,
        setup_script: str,
        prelude: str,
        wizard_vars: dict[str, Any],
    ) -> None:
        instructions = _instructions_after(
            setup_script,
            prelude,
            wizard_vars,
            _click("server-method", method)
            + _click("client", client_id)
            + _click("scope", "remote")
            + _click("remote-path", "cloudflared"),
        )
        assert _LEGACY_NOTICE_HEADLINE in instructions
        assert _LEGACY_NOTICE_GO_BACK in instructions
        assert working_marker not in instructions
