"""Shared fixtures for the setup-wizard JSDOM tests.

``test_astro_setup_js_behavior.py`` and ``test_astro_setup_wizard_platforms.py``
drive the same bundled wizard against the same minimal DOM; this module owns
the bundle, the production data and the DOM builder so neither test module
carries a copy. Fixtures stay in the test modules (thin wrappers over
``bundle_setup_script`` / ``load_wizard_vars``) so pytest finds them without
star imports.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ._js_harness import (
    HarnessResult,
    bundle_inline_script,
    extract_script_body,
    module_exports,
)

SITE = Path(__file__).resolve().parents[3] / "site" / "src"
SETUP_ASTRO = SITE / "pages" / "setup.astro"
WIZARD_DATA = SITE / "scripts" / "setup-wizard" / "data.ts"

# The ``data.ts`` exports the DOM builder and premise checks below read. The
# rework replaced the old connection/deployment model with a server-method +
# scope model, dropping ``connectionsData`` / ``deploymentData`` /
# ``httpOnlyClients`` entirely.
WIZARD_VAR_NAMES = [
    "clientsData",
    "platformsData",
    "stdioOnlyClients",
    "remoteOnlyClients",
]


def bundle_setup_script() -> str:
    """The shipped entry (`import { initSetupWizard } ...; initSetupWizard();`)
    bundled from the page's directory, so the tests run what the page runs.
    `base` comes from import.meta.env.BASE_URL at build time; the bundle gets
    the empty root here. Each test module wraps this in a module-scoped
    fixture named ``setup_script``."""
    entry = extract_script_body(
        SETUP_ASTRO.read_text(encoding="utf-8"),
        source_label=str(SETUP_ASTRO),
    )
    return bundle_inline_script(
        entry, SETUP_ASTRO.parent, define={"import.meta.env.BASE_URL": '""'}
    )


def load_wizard_vars() -> dict[str, Any]:
    """The production ``data.ts`` arrays the DOM builder and premise checks read."""
    return module_exports(WIZARD_DATA, WIZARD_VAR_NAMES)


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
    # `clientsData` (imported from data.ts) rather than this attr, but
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
