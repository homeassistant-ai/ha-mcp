"""Behavioral tests for the docs-site theme-toggle accessibility binding.

The ``data-purpose="theme-toggle"`` inline script in
``site/src/layouts/Layout.astro`` wires bidirectional sync between the Nav
header theme select, the Accessibility popover controls, and the underlying
localStorage / ``<html>`` attributes. The Layout head anti-FOUC script
already set the initial attributes; this module keeps them in sync for the
rest of the session and persists user changes.

These tests run the extracted binding script through JSDOM with a realistic
Nav.astro control structure seeded in ``initial_html``, then invoke DOM
events (input, change, click) and assert on localStorage writes and <html>
attribute mutations.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path

import pytest

from ._js_harness import run_script


def _extract_theme_toggle_body() -> str:
    """Return the body of the ``data-purpose="theme-toggle"`` inline script."""
    layout_path = (
        Path(__file__).resolve().parents[3]
        / "site"
        / "src"
        / "layouts"
        / "Layout.astro"
    )
    source = layout_path.read_text(encoding="utf-8")
    match = re.search(
        r'<script[^>]*\bdata-purpose\s*=\s*["\']theme-toggle["\'][^>]*>(.*?)</script>',
        source,
        re.DOTALL,
    )
    assert match is not None, "no theme-toggle script found in Layout.astro"
    return match.group(1)


@pytest.fixture(scope="module")
def theme_toggle_script() -> str:
    return _extract_theme_toggle_body()


@pytest.fixture
def a11y_controls_html() -> str:
    """Minimal Nav.astro structure the theme-toggle script binds to.

    Reproduces the essential control structure: #themeToggle select,
    #a11yPanel container with preset radios, font-size radios, custom color
    inputs, contrast warning, clear/reset buttons, and the storage-note
    paragraph.
    """
    return """
    <!DOCTYPE html><html><body>
      <select id="themeToggle">
        <option value="auto">Auto</option>
        <option value="light">Light</option>
        <option value="dark">Dark</option>
      </select>
      <button id="a11yToggle">Accessibility</button>
      <div id="a11yPanel">
        <p id="a11y-storage-note" hidden>Storage blocked</p>
        <fieldset>
          <input type="radio" name="a11y-preset" value="dark" />
          <input type="radio" name="a11y-preset" value="light" />
          <input type="radio" name="a11y-preset" value="auto" />
          <input type="radio" name="a11y-preset" value="paper" />
          <input type="radio" name="a11y-preset" value="gray" />
          <input type="radio" name="a11y-preset" value="contrast" />
        </fieldset>
        <fieldset>
          <input type="radio" name="a11y-font-size" value="100" />
          <input type="radio" name="a11y-font-size" value="115" />
          <input type="radio" name="a11y-font-size" value="130" />
          <input type="radio" name="a11y-font-size" value="150" />
        </fieldset>
        <input type="color" id="a11y-custom-bg" data-custom="bg" />
        <input type="color" id="a11y-custom-text" data-custom="text" />
        <input type="color" id="a11y-custom-accent" data-custom="accent" />
        <p id="a11y-contrast-warning" hidden>Low contrast warning</p>
        <button id="a11y-custom-clear">Clear</button>
        <button id="a11yReset">Reset</button>
      </div>
    </body></html>
    """


def test_custom_colors_input_persists_to_storage(
    theme_toggle_script: str, a11y_controls_html: str
) -> None:
    """Custom color input events persist to localStorage as JSON and set CSS vars.

    Dispatching an ``input`` event on ``#a11y-custom-bg`` with value
    ``#112233`` writes ``ha-mcp-custom-colors`` as JSON containing
    ``{"bg":"#112233"}`` and sets ``--bg`` and ``--surface-0`` CSS custom
    properties on ``<html>``.
    """
    prelude = """
    const _storage = {};
    const localStorageImpl = {
        getItem: (k) => _storage.hasOwnProperty(k) ? _storage[k] : null,
        setItem: (k, v) => { _storage[k] = v; },
        removeItem: (k) => { delete _storage[k]; },
    };
    Object.defineProperty(window, 'localStorage', { value: localStorageImpl, configurable: true });
    """
    invoke = """
    // Ensure DOMContentLoaded fires so the script attaches listeners.
    document.dispatchEvent(new Event('DOMContentLoaded'));
    const bgInput = document.getElementById('a11y-custom-bg');
    bgInput.value = '#112233';
    bgInput.dispatchEvent(new Event('input', {bubbles: true}));
    // Echo stored value into DOM for assertion.
    const stored = localStorage.getItem('ha-mcp-custom-colors') || 'NULL';
    document.body.setAttribute('data-test-custom', encodeURIComponent(stored));
    """
    result = run_script(
        theme_toggle_script,
        prelude=prelude,
        initial_html=a11y_controls_html,
        invoke=invoke,
        settle_ms=200,
    )
    assert not result.errors, f"errors: {result.errors}"
    # Extract stored value from DOM attribute.
    match = re.search(r'data-test-custom="([^"]*)"', result.dom)
    assert match, f"data-test-custom not found in dom={result.dom[:800]}"
    stored_encoded = match.group(1)
    stored_raw = urllib.parse.unquote(stored_encoded)
    assert stored_raw != "NULL", "stored value is NULL"
    stored_obj = json.loads(stored_raw)
    assert stored_obj["bg"] == "#112233", f"expected bg=#112233, got {stored_obj}"
    # Assert CSS vars set on <html> (this should happen even if storage failed).
    assert "--bg: #112233" in result.dom, f"expected --bg set in dom={result.dom[:800]}"
    assert "--surface-0: 17 34 51" in result.dom, (
        f"expected --surface-0 RGB channels in dom={result.dom[:800]}"
    )


def test_custom_colors_malformed_json_recovers(
    theme_toggle_script: str, a11y_controls_html: str
) -> None:
    """Malformed pre-seeded custom JSON is cleared on input and a fresh object written.

    Seeding ``ha-mcp-custom-colors`` with ``{not json`` and then dispatching
    an input event must not throw; the script starts fresh with a new JSON
    object containing just the new color.
    """
    storage = {"ha-mcp-custom-colors": "{not json"}
    prelude = f"""
    const _storage = {json.dumps(storage)};
    const localStorageImpl = {{
        getItem: (k) => _storage.hasOwnProperty(k) ? _storage[k] : null,
        setItem: (k, v) => {{ _storage[k] = v; }},
        removeItem: (k) => {{ delete _storage[k]; }},
    }};
    Object.defineProperty(window, 'localStorage', {{ value: localStorageImpl, configurable: true }});
    """
    invoke = """
    document.dispatchEvent(new Event('DOMContentLoaded'));
    const textInput = document.getElementById('a11y-custom-text');
    textInput.value = '#ff00ff';
    textInput.dispatchEvent(new Event('input', {bubbles: true}));
    // Echo stored value into DOM.
    const stored = localStorage.getItem('ha-mcp-custom-colors') || 'NULL';
    document.body.setAttribute('data-test-custom', encodeURIComponent(stored));
    """
    result = run_script(
        theme_toggle_script,
        prelude=prelude,
        initial_html=a11y_controls_html,
        invoke=invoke,
        settle_ms=200,
    )
    assert not result.errors, f"malformed JSON should not throw: {result.errors}"
    # Extract from DOM.
    match = re.search(r'data-test-custom="([^"]*)"', result.dom)
    assert match, "data-test-custom not found in dom"
    stored_encoded = match.group(1)
    stored_raw = urllib.parse.unquote(stored_encoded)
    assert stored_raw != "NULL", "stored value is NULL"
    stored_obj = json.loads(stored_raw)
    assert stored_obj.get("text") == "#ff00ff", (
        "expected fresh object with text=#ff00ff"
    )


def test_contrast_warning_table_driven(
    theme_toggle_script: str, a11y_controls_html: str
) -> None:
    """Contrast warning shows when bg/text ratio < 4.5:1, hidden otherwise.

    The script computes WCAG 2.x relative luminance and shows
    ``#a11y-contrast-warning`` when the ratio falls below 4.5. Test cases
    span well above, just above, just below, and well below the threshold.
    """
    test_cases = [
        # (bg, text, expected_hidden, ratio_comment)
        ("#000000", "#ffffff", True, "21:1 well above"),
        ("#777777", "#888888", False, "1.26:1 well below"),
        ("#333333", "#aaaaaa", True, "5.44:1 just above 4.5"),
        ("#555555", "#bbbbbb", False, "3.88:1 just below 4.5"),
    ]
    for bg, text, expected_hidden, comment in test_cases:
        custom_obj = {"bg": bg, "text": text}
        storage = {"ha-mcp-custom-colors": json.dumps(custom_obj)}
        prelude = f"""
        const _storage = {json.dumps(storage)};
        const localStorageImpl = {{
            getItem: (k) => _storage.hasOwnProperty(k) ? _storage[k] : null,
            setItem: (k, v) => {{ _storage[k] = v; }},
            removeItem: (k) => {{ delete _storage[k]; }},
        }};
        Object.defineProperty(window, 'localStorage', {{ value: localStorageImpl, configurable: true }});
        """
        invoke = """
        document.dispatchEvent(new Event('DOMContentLoaded'));
        // Trigger updateContrastWarning by dispatching input on bg.
        const bgInput = document.getElementById('a11y-custom-bg');
        bgInput.value = bgInput.value; // no change, just trigger
        bgInput.dispatchEvent(new Event('input', {bubbles: true}));
        """
        result = run_script(
            theme_toggle_script,
            prelude=prelude,
            initial_html=a11y_controls_html,
            invoke=invoke,
            settle_ms=200,
        )
        assert not result.errors, f"{comment}: errors={result.errors}"
        warn_el_match = re.search(
            r'<p[^>]*id="a11y-contrast-warning"([^>]*)>',
            result.dom,
        )
        assert warn_el_match, f"{comment}: #a11y-contrast-warning not found in DOM"
        attrs = warn_el_match.group(1)
        has_hidden = "hidden" in attrs
        assert has_hidden == expected_hidden, (
            f"{comment}: bg={bg} text={text} expected hidden={expected_hidden}, "
            f"got hidden={has_hidden}"
        )


def test_preset_round_trip_paper(
    theme_toggle_script: str, a11y_controls_html: str
) -> None:
    """Clicking the paper preset radio stores the triple and keeps the radio checked.

    The ``paper`` preset sets ``theme=light``, ``shade=paper``,
    ``contrast=normal`` in a single click. After the change event, the
    stored triple must match and the paper radio must remain checked.
    """
    prelude = """
    const _storage = {};
    const localStorageImpl = {
        getItem: (k) => _storage.hasOwnProperty(k) ? _storage[k] : null,
        setItem: (k, v) => { _storage[k] = v; },
        removeItem: (k) => { delete _storage[k]; },
    };
    Object.defineProperty(window, 'localStorage', { value: localStorageImpl, configurable: true });
    """
    invoke = """
    document.dispatchEvent(new Event('DOMContentLoaded'));
    const paperRadio = document.querySelector('input[name="a11y-preset"][value="paper"]');
    paperRadio.checked = true;
    paperRadio.dispatchEvent(new Event('change', {bubbles: true}));
    // Echo storage into DOM.
    document.body.setAttribute('data-test-theme', localStorage.getItem('ha-mcp-theme') || 'NULL');
    document.body.setAttribute('data-test-shade', localStorage.getItem('ha-mcp-shade') || 'NULL');
    document.body.setAttribute('data-test-contrast', localStorage.getItem('ha-mcp-contrast') || 'NULL');
    document.body.setAttribute('data-test-paper-checked', paperRadio.checked ? 'true' : 'false');
    """
    result = run_script(
        theme_toggle_script,
        prelude=prelude,
        initial_html=a11y_controls_html,
        invoke=invoke,
        settle_ms=200,
    )
    assert not result.errors, f"errors: {result.errors}"
    # Extract from DOM.
    theme_match = re.search(r'data-test-theme="([^"]*)"', result.dom)
    shade_match = re.search(r'data-test-shade="([^"]*)"', result.dom)
    contrast_match = re.search(r'data-test-contrast="([^"]*)"', result.dom)
    paper_checked_match = re.search(r'data-test-paper-checked="([^"]*)"', result.dom)
    assert theme_match and shade_match and contrast_match and paper_checked_match, (
        "missing test attributes in DOM"
    )
    theme_val = theme_match.group(1)
    shade_val = shade_match.group(1)
    contrast_val = contrast_match.group(1)
    paper_checked = paper_checked_match.group(1) == "true"
    assert theme_val == "light", f"expected theme=light, got {theme_val}"
    assert shade_val == "paper", f"expected shade=paper, got {shade_val}"
    assert contrast_val == "normal", f"expected contrast=normal, got {contrast_val}"
    assert paper_checked is True, f"expected paper radio checked, got {paper_checked}"


def test_preset_no_match_unchecks_all(
    theme_toggle_script: str, a11y_controls_html: str
) -> None:
    """Seeding a non-preset triple (e.g. dark+paper) leaves all preset radios unchecked.

    If the stored triple does not match any preset (``dark`` theme with
    ``paper`` shade is not a defined preset), the ``reflectPreset()``
    function must leave every ``a11y-preset`` radio unchecked.
    """
    storage = {
        "ha-mcp-theme": "dark",
        "ha-mcp-shade": "paper",
        "ha-mcp-contrast": "normal",
    }
    prelude = f"""
    const _storage = {json.dumps(storage)};
    const localStorageImpl = {{
        getItem: (k) => _storage.hasOwnProperty(k) ? _storage[k] : null,
        setItem: (k, v) => {{ _storage[k] = v; }},
        removeItem: (k) => {{ delete _storage[k]; }},
    }};
    Object.defineProperty(window, 'localStorage', {{ value: localStorageImpl, configurable: true }});
    """
    invoke = """
    document.dispatchEvent(new Event('DOMContentLoaded'));
    // Just let the script's DOMContentLoaded reflectPreset() run.
    const presetRadios = document.querySelectorAll('input[name="a11y-preset"]');
    const anyChecked = Array.from(presetRadios).some(r => r.checked);
    document.body.setAttribute('data-test-any-checked', anyChecked ? 'true' : 'false');
    """
    result = run_script(
        theme_toggle_script,
        prelude=prelude,
        initial_html=a11y_controls_html,
        invoke=invoke,
        settle_ms=200,
    )
    assert not result.errors, f"errors: {result.errors}"
    match = re.search(r'data-test-any-checked="([^"]*)"', result.dom)
    assert match, "data-test-any-checked not found in DOM"
    any_checked = match.group(1) == "true"
    assert any_checked is False, (
        f"expected no preset radios checked for dark+paper triple, got {any_checked}"
    )


def test_font_size_clamp_parity(
    theme_toggle_script: str, a11y_controls_html: str
) -> None:
    """Font-size clamping for out-of-range values matches across anti-FOUC and runtime paths.

    The anti-FOUC script (data-purpose="anti-fouc") and the runtime
    ``applyFontSize`` arrow function (mirrored in both Layout.astro
    theme-toggle and src/ha_mcp/settings.js) both clamp
    invalid/out-of-range values to empty string (clearing the inline style).
    Test that ``90``, ``130``, ``200``, and ``abc`` all produce the same
    result: anti-FOUC path sets fontSize to empty string or "20.8px" for
    130; runtime path does the same.
    """
    layout_path = (
        Path(__file__).resolve().parents[3]
        / "site"
        / "src"
        / "layouts"
        / "Layout.astro"
    )
    settings_path = (
        Path(__file__).resolve().parents[3] / "src" / "ha_mcp" / "settings.js"
    )
    layout_source = layout_path.read_text(encoding="utf-8")
    settings_source = settings_path.read_text(encoding="utf-8")
    # Extract anti-FOUC body from Layout.astro.
    anti_fouc_match = re.search(
        r'<script[^>]*\bdata-purpose\s*=\s*["\']anti-fouc["\'][^>]*>(.*?)</script>',
        layout_source,
        re.DOTALL,
    )
    assert anti_fouc_match, "no anti-fouc script found in Layout.astro"
    anti_fouc_body = anti_fouc_match.group(1)
    # Extract the applyFontSize arrow function from Layout.astro theme-toggle script.
    apply_font_size_layout_match = re.search(
        r"const applyFontSize = \(pct\) => \{[^}]*\};",
        theme_toggle_script,
        re.DOTALL,
    )
    assert apply_font_size_layout_match, (
        "no applyFontSize function found in Layout.astro theme-toggle"
    )
    apply_font_size_layout_fn = apply_font_size_layout_match.group(0)
    # Extract the applyFontSize arrow function from settings.js.
    apply_font_size_settings_match = re.search(
        r"const applyFontSize = \(pct\) => \{[^}]*\};",
        settings_source,
        re.DOTALL,
    )
    assert apply_font_size_settings_match, (
        "no applyFontSize function found in settings.js"
    )
    apply_font_size_settings_fn = apply_font_size_settings_match.group(0)

    test_cases = [
        ("90", ""),  # Below min (100).
        ("130", "20.8px"),  # Valid in-range.
        ("200", ""),  # Above max (150).
        ("abc", ""),  # Non-numeric.
    ]
    for pct_input, expected_style in test_cases:
        # (a) Anti-FOUC path.
        storage_anti_fouc = {"ha-mcp-font-size": pct_input}
        prelude_anti_fouc = f"""
        const _storage = {json.dumps(storage_anti_fouc)};
        const localStorageImpl = {{
            getItem: (k) => _storage.hasOwnProperty(k) ? _storage[k] : null,
            setItem: (k, v) => {{ _storage[k] = v; }},
            removeItem: (k) => {{ delete _storage[k]; }},
        }};
        Object.defineProperty(window, 'localStorage', {{ value: localStorageImpl, configurable: true }});
        """
        result_anti_fouc = run_script(
            anti_fouc_body,
            prelude=prelude_anti_fouc,
            settle_ms=100,
        )
        assert not result_anti_fouc.errors, (
            f"anti-fouc {pct_input}: errors={result_anti_fouc.errors}"
        )
        # Parse the <html style="..."> attribute.
        html_match = re.search(r'<html[^>]*style="([^"]*)"', result_anti_fouc.dom)
        anti_fouc_style = html_match.group(1) if html_match else ""
        if expected_style:
            assert f"font-size: {expected_style}" in anti_fouc_style, (
                f"anti-fouc {pct_input}: expected font-size: {expected_style}, got {anti_fouc_style}"
            )
        else:
            # Empty style or no font-size.
            assert "font-size:" not in anti_fouc_style, (
                f"anti-fouc {pct_input}: expected no font-size, got {anti_fouc_style}"
            )

        # (b) Runtime path from Layout.astro theme-toggle.
        prelude_layout = f"""
        const root = document.documentElement;
        {apply_font_size_layout_fn}
        applyFontSize({json.dumps(pct_input)});
        document.body.setAttribute('data-test-layout-style', root.style.fontSize || 'EMPTY');
        """
        result_layout = run_script(
            "",  # No main script body needed; prelude does it all.
            prelude=prelude_layout,
            settle_ms=100,
        )
        assert not result_layout.errors, (
            f"layout {pct_input}: errors={result_layout.errors}"
        )
        layout_match = re.search(r'data-test-layout-style="([^"]*)"', result_layout.dom)
        assert layout_match, f"data-test-layout-style not found for {pct_input}"
        layout_style = layout_match.group(1)
        if layout_style == "EMPTY":
            layout_style = ""
        assert layout_style == expected_style, (
            f"layout {pct_input}: expected {expected_style!r}, got {layout_style!r}"
        )

        # (c) Runtime path from settings.js.
        prelude_settings = f"""
        const root = document.documentElement;
        {apply_font_size_settings_fn}
        applyFontSize({json.dumps(pct_input)});
        document.body.setAttribute('data-test-settings-style', root.style.fontSize || 'EMPTY');
        """
        result_settings = run_script(
            "",  # No main script body needed; prelude does it all.
            prelude=prelude_settings,
            settle_ms=100,
        )
        assert not result_settings.errors, (
            f"settings {pct_input}: errors={result_settings.errors}"
        )
        settings_match = re.search(
            r'data-test-settings-style="([^"]*)"', result_settings.dom
        )
        assert settings_match, f"data-test-settings-style not found for {pct_input}"
        settings_style = settings_match.group(1)
        if settings_style == "EMPTY":
            settings_style = ""
        assert settings_style == expected_style, (
            f"settings {pct_input}: expected {expected_style!r}, got {settings_style!r}"
        )


def test_reset_clears_all_and_clear_only_custom(
    theme_toggle_script: str, a11y_controls_html: str
) -> None:
    """Reset button clears all prefs to defaults; clear button only clears custom colors.

    Seed all five ``ha-mcp-*`` keys with non-default values. Clicking
    ``#a11y-custom-clear`` must clear only ``ha-mcp-custom-colors`` (to
    empty string); the other four stay unchanged. Clicking ``#a11yReset``
    must reset all five to defaults: ``theme=auto``, ``font-size=100``,
    ``contrast=normal``, ``shade=off-white``, ``custom=''``.
    """
    storage = {
        "ha-mcp-theme": "light",
        "ha-mcp-font-size": "130",
        "ha-mcp-contrast": "high",
        "ha-mcp-shade": "paper",
        "ha-mcp-custom-colors": json.dumps({"bg": "#112233"}),
    }
    # Test (a): clear custom.
    prelude_clear = f"""
    const _storage = {json.dumps(storage)};
    const localStorageImpl = {{
        getItem: (k) => _storage.hasOwnProperty(k) ? _storage[k] : null,
        setItem: (k, v) => {{ _storage[k] = v; }},
        removeItem: (k) => {{ delete _storage[k]; }},
    }};
    Object.defineProperty(window, 'localStorage', {{ value: localStorageImpl, configurable: true }});
    """
    invoke_clear = """
    document.dispatchEvent(new Event('DOMContentLoaded'));
    const clearBtn = document.getElementById('a11y-custom-clear');
    clearBtn.click();
    const theme = localStorage.getItem('ha-mcp-theme');
    const fontSize = localStorage.getItem('ha-mcp-font-size');
    const contrast = localStorage.getItem('ha-mcp-contrast');
    const shade = localStorage.getItem('ha-mcp-shade');
    const custom = localStorage.getItem('ha-mcp-custom-colors');
    document.body.setAttribute('data-test-theme', theme !== null ? theme : 'NULL');
    document.body.setAttribute('data-test-font-size', fontSize !== null ? fontSize : 'NULL');
    document.body.setAttribute('data-test-contrast', contrast !== null ? contrast : 'NULL');
    document.body.setAttribute('data-test-shade', shade !== null ? shade : 'NULL');
    document.body.setAttribute('data-test-custom', custom !== null ? custom : 'NULL');
    """
    result_clear = run_script(
        theme_toggle_script,
        prelude=prelude_clear,
        initial_html=a11y_controls_html,
        invoke=invoke_clear,
        settle_ms=200,
    )
    assert not result_clear.errors, f"clear: errors={result_clear.errors}"
    theme_clear_match = re.search(r'data-test-theme="([^"]*)"', result_clear.dom)
    font_size_clear_match = re.search(
        r'data-test-font-size="([^"]*)"', result_clear.dom
    )
    contrast_clear_match = re.search(r'data-test-contrast="([^"]*)"', result_clear.dom)
    shade_clear_match = re.search(r'data-test-shade="([^"]*)"', result_clear.dom)
    custom_clear_match = re.search(r'data-test-custom="([^"]*)"', result_clear.dom)
    assert all(
        [
            theme_clear_match,
            font_size_clear_match,
            contrast_clear_match,
            shade_clear_match,
            custom_clear_match,
        ]
    ), "missing test attributes in clear DOM"
    assert theme_clear_match is not None
    assert font_size_clear_match is not None
    assert contrast_clear_match is not None
    assert shade_clear_match is not None
    assert custom_clear_match is not None
    theme_clear = theme_clear_match.group(1)
    font_size_clear = font_size_clear_match.group(1)
    contrast_clear = contrast_clear_match.group(1)
    shade_clear = shade_clear_match.group(1)
    custom_clear = custom_clear_match.group(1)
    assert theme_clear == "light", "clear: theme should stay light"
    assert font_size_clear == "130", "clear: fontSize should stay 130"
    assert contrast_clear == "high", "clear: contrast should stay high"
    assert shade_clear == "paper", "clear: shade should stay paper"
    assert custom_clear == "", "clear: custom should be empty"

    # Test (b): reset all.
    prelude_reset = f"""
    const _storage = {json.dumps(storage)};
    const localStorageImpl = {{
        getItem: (k) => _storage.hasOwnProperty(k) ? _storage[k] : null,
        setItem: (k, v) => {{ _storage[k] = v; }},
        removeItem: (k) => {{ delete _storage[k]; }},
    }};
    Object.defineProperty(window, 'localStorage', {{ value: localStorageImpl, configurable: true }});
    """
    invoke_reset = """
    document.dispatchEvent(new Event('DOMContentLoaded'));
    const resetBtn = document.getElementById('a11yReset');
    resetBtn.click();
    const theme = localStorage.getItem('ha-mcp-theme');
    const fontSize = localStorage.getItem('ha-mcp-font-size');
    const contrast = localStorage.getItem('ha-mcp-contrast');
    const shade = localStorage.getItem('ha-mcp-shade');
    const custom = localStorage.getItem('ha-mcp-custom-colors');
    document.body.setAttribute('data-test-theme', theme !== null ? theme : 'NULL');
    document.body.setAttribute('data-test-font-size', fontSize !== null ? fontSize : 'NULL');
    document.body.setAttribute('data-test-contrast', contrast !== null ? contrast : 'NULL');
    document.body.setAttribute('data-test-shade', shade !== null ? shade : 'NULL');
    document.body.setAttribute('data-test-custom', custom !== null ? custom : 'NULL');
    """
    result_reset = run_script(
        theme_toggle_script,
        prelude=prelude_reset,
        initial_html=a11y_controls_html,
        invoke=invoke_reset,
        settle_ms=200,
    )
    assert not result_reset.errors, f"reset: errors={result_reset.errors}"
    theme_reset_match = re.search(r'data-test-theme="([^"]*)"', result_reset.dom)
    font_size_reset_match = re.search(
        r'data-test-font-size="([^"]*)"', result_reset.dom
    )
    contrast_reset_match = re.search(r'data-test-contrast="([^"]*)"', result_reset.dom)
    shade_reset_match = re.search(r'data-test-shade="([^"]*)"', result_reset.dom)
    custom_reset_match = re.search(r'data-test-custom="([^"]*)"', result_reset.dom)
    assert all(
        [
            theme_reset_match,
            font_size_reset_match,
            contrast_reset_match,
            shade_reset_match,
            custom_reset_match,
        ]
    ), "missing test attributes in reset DOM"
    assert theme_reset_match is not None
    assert font_size_reset_match is not None
    assert contrast_reset_match is not None
    assert shade_reset_match is not None
    assert custom_reset_match is not None
    theme_reset = theme_reset_match.group(1)
    font_size_reset = font_size_reset_match.group(1)
    contrast_reset = contrast_reset_match.group(1)
    shade_reset = shade_reset_match.group(1)
    custom_reset = custom_reset_match.group(1)
    assert theme_reset == "auto", "reset: theme should be auto"
    assert font_size_reset == "100", "reset: fontSize should be 100"
    assert contrast_reset == "normal", "reset: contrast should be normal"
    assert shade_reset == "off-white", "reset: shade should be off-white"
    assert custom_reset == "", "reset: custom should be empty"


def test_auto_matchmedia_flip(
    theme_toggle_script: str, a11y_controls_html: str
) -> None:
    """Auto theme respects matchMedia change events and flips data-theme dynamically.

    When ``theme=auto``, the script listens to
    ``matchMedia('(prefers-color-scheme: light)')`` change events. Seeding
    ``theme=auto`` and running the script with a stub that records the
    listener and exposes ``window.__flipMql(matches)`` to simulate an OS
    preference flip must update ``data-theme`` on ``<html>`` accordingly.
    """
    storage = {"ha-mcp-theme": "auto"}
    prelude = f"""
    const _storage = {json.dumps(storage)};
    const localStorageImpl = {{
        getItem: (k) => _storage.hasOwnProperty(k) ? _storage[k] : null,
        setItem: (k, v) => {{ _storage[k] = v; }},
        removeItem: (k) => {{ delete _storage[k]; }},
    }};
    Object.defineProperty(window, 'localStorage', {{ value: localStorageImpl, configurable: true }});
    // Stub matchMedia to record the listener and allow manual flip.
    let _mqlListener = null;
    let _mqlMatches = false;
    window.matchMedia = (q) => {{
        const obj = {{
            get matches() {{ return _mqlMatches; }},
            media: q,
            addEventListener: (event, fn) => {{ if (event === 'change') _mqlListener = fn; }},
            removeEventListener: () => {{}},
        }};
        return obj;
    }};
    window.__flipMql = (matches) => {{
        _mqlMatches = matches;
        if (_mqlListener) {{
            _mqlListener({{ matches }});
        }}
    }};
    """
    invoke = """
    document.dispatchEvent(new Event('DOMContentLoaded'));
    // Manually trigger initial theme application (anti-FOUC would have done this).
    // The theme-toggle script doesn't call applyTheme on init, only on change.
    const root = document.documentElement;
    const mql = window.matchMedia('(prefers-color-scheme: light)');
    // Set initial state based on mql.matches (false → dark).
    root.setAttribute('data-theme', mql.matches ? 'light' : 'dark');
    document.body.setAttribute('data-test-initial-theme', root.getAttribute('data-theme') || 'NULL');
    // Flip to matches=true → should trigger listener and update to light.
    window.__flipMql(true);
    document.body.setAttribute('data-test-after-flip-theme', root.getAttribute('data-theme') || 'NULL');
    """
    result = run_script(
        theme_toggle_script,
        prelude=prelude,
        initial_html=a11y_controls_html,
        invoke=invoke,
        settle_ms=200,
    )
    assert not result.errors, f"errors: {result.errors}"
    initial_theme_match = re.search(r'data-test-initial-theme="([^"]*)"', result.dom)
    after_flip_theme_match = re.search(
        r'data-test-after-flip-theme="([^"]*)"', result.dom
    )
    assert initial_theme_match and after_flip_theme_match, (
        "missing theme test attributes in DOM"
    )
    initial_theme = initial_theme_match.group(1)
    after_flip_theme = after_flip_theme_match.group(1)
    assert initial_theme == "dark", (
        f"expected initial data-theme=dark for matches=false, got {initial_theme}"
    )
    assert after_flip_theme == "light", (
        f"expected data-theme=light after flip to matches=true, got {after_flip_theme}"
    )


def test_storage_note_on_blocked_setitem(
    theme_toggle_script: str, a11y_controls_html: str
) -> None:
    """Blocked localStorage.setItem un-hides #a11y-storage-note via __haMcpPrefsHook.

    The theme-toggle module defines ``window.__haMcpPrefsHook(pref, value,
    stored)`` which un-hides ``#a11y-storage-note`` when ``stored=false``.
    Seeding initial storage (so the script loads without errors), then
    overriding ``localStorage.setItem`` to throw, and dispatching a preset
    radio change must trigger the hook and un-hide the note.
    """
    storage = {"ha-mcp-theme": "auto"}
    prelude = f"""
    const _storage = {json.dumps(storage)};
    const localStorageImpl = {{
        getItem: (k) => _storage.hasOwnProperty(k) ? _storage[k] : null,
        setItem: (k, v) => {{ throw new Error('storage blocked'); }},
        removeItem: (k) => {{ delete _storage[k]; }},
    }};
    Object.defineProperty(window, 'localStorage', {{ value: localStorageImpl, configurable: true }});
    """
    invoke = """
    document.dispatchEvent(new Event('DOMContentLoaded'));
    const lightRadio = document.querySelector('input[name="a11y-preset"][value="light"]');
    lightRadio.checked = true;
    lightRadio.dispatchEvent(new Event('change', {bubbles: true}));
    const note = document.getElementById('a11y-storage-note');
    document.body.setAttribute('data-test-note-hidden', note.hidden ? 'true' : 'false');
    """
    result = run_script(
        theme_toggle_script,
        prelude=prelude,
        initial_html=a11y_controls_html,
        invoke=invoke,
        settle_ms=200,
    )
    assert not result.errors, f"errors: {result.errors}"
    note_hidden_match = re.search(r'data-test-note-hidden="([^"]*)"', result.dom)
    assert note_hidden_match, "data-test-note-hidden not found in DOM"
    note_hidden = note_hidden_match.group(1) == "true"
    assert note_hidden is False, (
        f"expected #a11y-storage-note.hidden=false after blocked setItem, got {note_hidden}"
    )
