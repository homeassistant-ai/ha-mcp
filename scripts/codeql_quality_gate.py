#!/usr/bin/env python3
"""Parse a CodeQL SARIF file, report findings, and gate on them.

CodeQL's Code Quality preview (the GitHub Settings → Security → Code quality
page) is only available to Team/Enterprise Cloud org plans, so it cannot be
enabled on this repo's free org. This script provides an equivalent gate by
running the ``<language>-code-quality.qls`` suite via the CodeQL CLI in CI
and failing the job when any finding remains. The workflow also feeds the
default ``<language>-code-scanning.qls`` security suite through the same
gate, because GitHub default setup only analyzes master post-merge and does
not block PRs (see the header of codeql-quality.yml).

Usage:
    codeql_quality_gate.py <quality.sarif>

Exits 1 if the SARIF contains any results, 0 otherwise. A grouped, sorted
finding list is printed to stdout and (when running in Actions) appended to
the job summary.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# Findings under these path prefixes are dropped before gating. These are
# vendored / third-party trees we do not own and ruff already excludes them
# (see ``extend-exclude`` in pyproject.toml). Keep this in sync with that list.
PATHS_IGNORE: tuple[str, ...] = ("tests/initial_test_state/",)

# Per-finding allowlist for verified false positives and intentional patterns
# that cannot be cleared without breaking or contorting correct code. Each entry
# is (rule_id, path_fragment, message_substring, reason). A finding is suppressed
# only when all three of rule/path/message match — keep this list tight and the
# reason current. Suppressed findings are reported (not silently dropped). Scope
# every entry to a specific file + message signature so a genuinely new finding
# of the same rule elsewhere still fails the gate.
ALLOWLIST: tuple[tuple[str, str, str, str], ...] = (
    (
        "py/unused-global-variable",
        "tests/src/unit/_embedded_stubs.py",
        "_INSTALLED",
        "Cross-invocation use: install() sets the module-level flag under a "
        "'global' declaration so a second call returns early. CodeQL's "
        "single-pass dead-store analysis misses the next-call read at the top "
        "of install(), so the assignment looks dead.",
    ),
    # NOTE: CodeQL emits the same generic message for every instance of
    # py/ineffectual-statement, so the two entries below are PATH-WIDE for
    # that rule (a future genuinely-dead statement in these files would be
    # suppressed too). Accepted: both files are small and the rationale
    # names the exact suppressed statements - re-audit if either file grows.
    (
        "py/ineffectual-statement",
        "custom_components/ha_mcp_tools/embedded_entry.py",
        "This statement has no effect",
        "False positive on a bare 'await task' inside contextlib.suppress: "
        "awaiting a cancelled task IS the effect (it waits for the task to "
        "finish unwinding before teardown continues).",
    ),
    (
        "py/ineffectual-statement",
        "custom_components/ha_mcp_tools/embedded_server.py",
        "This statement has no effect",
        "False positive on bare 'await serve_task' / 'await stop_task' inside "
        "contextlib.suppress: the await drives the cancelled task to "
        "completion, which is the required shutdown-sequencing effect.",
    ),
    (
        "py/unused-global-variable",
        "src/ha_mcp/__main__.py",
        "_shutdown_in_progress",
        "Cross-invocation use: set True on the first signal, read on the next to "
        "force-exit. CodeQL's single-pass dead-store analysis misses the "
        "second-signal read, so the assignment looks dead.",
    ),
    (
        "py/unused-global-variable",
        "custom_components/ha_mcp_tools/embedded_server.py",
        "_PENDING_INSTALL_DONE",
        "Cross-method use: _async_run_tracked_install_job registers/clears the "
        "slot under a 'global' declaration and _async_wait_for_pending_install "
        "reads it on the NEXT bring-up (a different coroutine). CodeQL's "
        "single-pass dead-store analysis misses the cross-method read, so the "
        "assignment looks dead.",
    ),
    (
        "py/unused-global-variable",
        "homeassistant-addon-webhook-proxy/mcp_proxy/__init__.py",
        "_LOGGER_LEVEL_RAISED",
        "Cross-invocation use: set when the debug toggle raises the logger to "
        "INFO on one async_setup_entry, then read on a later one (a config-entry "
        "reload) to undo only our own raise. CodeQL's single-pass dead-store "
        "analysis misses the cross-invocation read, so the assignment looks dead.",
    ),
    (
        "py/unused-global-variable",
        "homeassistant-addon-webhook-proxy-dev/mcp_proxy_dev/__init__.py",
        "_LOGGER_LEVEL_RAISED",
        "Cross-invocation use (dev flavor, identical code to stable): set when "
        "the debug toggle raises the logger to INFO on one async_setup_entry, "
        "then read on a later one (a config-entry reload) to undo only our own "
        "raise. CodeQL's single-pass dead-store analysis misses the "
        "cross-invocation read, so the assignment looks dead.",
    ),
    (
        "py/unused-global-variable",
        "src/ha_mcp/settings_ui/_tools_meta.py",
        "_VALID_STATES",
        "Cross-module use: _tools_meta.py is a leaf module in the settings_ui "
        "split, so this frozenset is imported and read by _handlers_tools.py's "
        "_coerce_tool_states. CodeQL's single-file analysis misses the "
        "cross-module import, so the declaration looks dead.",
    ),
    (
        "py/unused-global-variable",
        "src/ha_mcp/tools/util_helpers.py",
        "_SERVICE_TO_STATE",
        "Cross-module use: util_helpers is the leaf module that owns the single "
        "_SERVICE_TO_STATE map, imported and read by tools_service.py (ha_call_service) "
        "and device_control.py (ha_bulk_control) as the confirmation-state hint. "
        "CodeQL's single-file analysis misses the cross-module import, so the "
        "declaration looks dead.",
    ),
    (
        "py/unused-import",
        "packaging/binary/pyinstaller_hooks/runtime_hook.py",
        "idna",
        "Intentional side-effect import: registers the idna codec at startup and "
        "forces PyInstaller to bundle it. Rewriting it risks the binary build.",
    ),
    (
        "py/unused-import",
        "packaging/binary/pyinstaller_hooks/runtime_hook.py",
        "encodings",
        "Intentional side-effect import: registers the stdlib encodings.idna "
        "codec at startup. Rewriting it risks the binary build.",
    ),
    (
        "py/catch-base-exception",
        "homeassistant-addon/start.py",
        "BaseException",
        "Intentional top-level supervisor handler: must catch SystemExit to emit "
        "a clean add-on exit code; KeyboardInterrupt is handled by the clause "
        "above. Matches how the codebase suppresses other deliberate catches.",
    ),
    # NOTE: CodeQL emits the same generic message for every py/mixed-returns
    # finding, so this entry is PATH-WIDE for server.py (a future genuinely
    # mixed-returns function in this file would be suppressed too). Accepted:
    # the rationale below names the exact functions - re-audit if the file grows.
    (
        "py/mixed-returns",
        "src/ha_mcp/server.py",
        "Mixing implicit and explicit returns",
        "False positive on _skill_guide_degraded_response and "
        "_read_skill_file_content's success-branch-vs-raise shape: the non-return "
        "branch in each always goes through raise_tool_error, typed '-> NoReturn' "
        "(see helpers.py), so there is no implicit None return for CodeQL's "
        "heuristic to have correctly caught. mypy already confirms this file is "
        "clean under that typing.",
    ),
    # ---- Security-suite entries (the workflow also gates the default
    # <language>-code-scanning suites; see codeql-quality.yml header). Entries
    # whose message substring is generic ("as clear text", "hashing algorithm")
    # are PATH-WIDE for that rule+file — a future finding of the same rule in
    # the same file would be suppressed too. Accepted for the same reason as
    # the quality entries above: the files are small and each reason names the
    # exact intended pattern. Re-audit when one of these files grows.
    (
        "py/clear-text-logging-sensitive-data",
        "custom_components/ha_mcp_tools/embedded_setup.py",
        "as clear text",
        "Deliberate admin-only connect instructions: the startup log prints the "
        "legacy-OAuth Client ID/Secret so the admin can paste them into an MCP "
        "client. The SECURITY note from the #1880 review in this file governs "
        "the pattern: credentials are withheld while a rotation is pending and "
        "never placed in the persistent notification all users can see.",
    ),
    (
        "py/clear-text-logging-sensitive-data",
        "homeassistant-addon-webhook-proxy/mcp_proxy/__init__.py",
        "as clear text",
        "False positive: the log line emits oauth_provider.client_id_masked(), "
        "not the raw value; CodeQL tracks taint through the masking helper.",
    ),
    (
        "py/clear-text-logging-sensitive-data",
        "homeassistant-addon-webhook-proxy-dev/mcp_proxy_dev/__init__.py",
        "as clear text",
        "False positive (dev flavor, identical code to stable): the log line "
        "emits client_id_masked(), not the raw value; CodeQL tracks taint "
        "through the masking helper.",
    ),
    (
        "py/clear-text-logging-sensitive-data",
        "homeassistant-addon-webhook-proxy/start.py",
        "as clear text",
        "Deliberate add-on startup log: prints the connect URL and legacy-OAuth "
        "credentials to the Supervisor add-on log (admin-only) as the user's "
        "setup instructions — the add-on-side mirror of embedded_setup.py's "
        "admin-only connect log.",
    ),
    (
        "py/clear-text-logging-sensitive-data",
        "homeassistant-addon-webhook-proxy-dev/start.py",
        "as clear text",
        "Deliberate add-on startup log (dev flavor, identical code to stable): "
        "prints the connect URL and legacy-OAuth credentials to the Supervisor "
        "add-on log (admin-only) as the user's setup instructions.",
    ),
    (
        "py/clear-text-logging-sensitive-data",
        "src/ha_mcp/stdio_settings_sidecar.py",
        "as clear text",
        "Deliberate: logs the sidecar's own loopback settings URL "
        "(http://127.0.0.1:<port><secret_path>/settings) so the local operator "
        "can open it. Local-process log/stderr only; the URL is unreachable off "
        "the host.",
    ),
    (
        "py/clear-text-logging-sensitive-data",
        "tests/test_env_manager.py",
        "as clear text",
        "Interactive test-environment helper printing the seeded throwaway "
        "credentials of the disposable HA test container (tests/test_constants.py). "
        "Printing them for copy-paste is the tool's purpose.",
    ),
    (
        "py/clear-text-storage-sensitive-data",
        "homeassistant-addon-webhook-proxy/mcp_proxy/oauth.py",
        "as clear text",
        "Warned fallback: the primary path writes the signing key via "
        "_atomic_write_0600; the flagged plain write only runs when the "
        "filesystem cannot honor 0600 and it logs a warning. Persisting the key "
        "is the feature (it must survive restarts).",
    ),
    (
        "py/clear-text-storage-sensitive-data",
        "homeassistant-addon-webhook-proxy-dev/mcp_proxy_dev/oauth.py",
        "as clear text",
        "Warned fallback (dev flavor, identical code to stable): plain write of "
        "the signing key only when the filesystem cannot honor 0600, with a "
        "warning. Persistence is the feature.",
    ),
    (
        "py/clear-text-storage-sensitive-data",
        "homeassistant-addon-webhook-proxy/start.py",
        "as clear text",
        "Warned fallbacks: both the creds file and the proxy-config handoff "
        "file write via _atomic_write_0600; the flagged plain writes only run "
        "when the filesystem cannot honor 0600 and each logs a warning. "
        "Persistence is the feature.",
    ),
    (
        "py/clear-text-storage-sensitive-data",
        "homeassistant-addon-webhook-proxy-dev/start.py",
        "as clear text",
        "Warned fallbacks (dev flavor): both the creds file and the "
        "proxy-config handoff file write via _atomic_write_0600; the flagged "
        "plain writes only run when the filesystem cannot honor 0600 and each "
        "logs a warning. Persistence is the feature.",
    ),
    (
        "py/weak-sensitive-data-hashing",
        "custom_components/ha_mcp_tools/oauth_legacy.py",
        "hashing algorithm (SHA256)",
        "Not password storage: SHA256 builds a change-detection fingerprint of "
        "the OAuth identity bound to the root views (machine-generated client "
        "secret + 256-bit signing key) to decide when routes must be rebound. "
        "KDFs exist to slow guessing of low-entropy human passwords; a "
        "fingerprint of high-entropy random material has no guessing surface.",
    ),
    (
        "py/weak-sensitive-data-hashing",
        "homeassistant-addon-webhook-proxy/mcp_proxy/__init__.py",
        "hashing algorithm (SHA256)",
        "Not password storage: same _oauth_route_fingerprint helper as "
        "oauth_legacy.py — a SHA256 change-detection fingerprint of "
        "machine-generated high-entropy credentials, not a stored password hash.",
    ),
    (
        "py/weak-sensitive-data-hashing",
        "homeassistant-addon-webhook-proxy-dev/mcp_proxy_dev/__init__.py",
        "hashing algorithm (SHA256)",
        "Not password storage (dev flavor, identical code to stable): SHA256 "
        "change-detection fingerprint of machine-generated high-entropy "
        "credentials, not a stored password hash.",
    ),
    (
        "py/bad-tag-filter",
        "tests/src/unit/_js_harness.py",
        "does not match upper case",
        "Not a sanitizer: the JSDOM harness extracts <script> bodies from "
        "repo-authored, lowercase templates for parse/behaviour testing; "
        "untrusted HTML never flows through it.",
    ),
    (
        "py/incomplete-url-substring-sanitization",
        "tests/src/unit/test_best_practice_checker.py",
        "may be at an arbitrary position",
        "Test assertion, not URL validation: checks that a warning message "
        "mentions the configured skill-prefix host.",
    ),
    (
        "py/incomplete-url-substring-sanitization",
        "tests/src/unit/test_browser_landing.py",
        "may be at an arbitrary position",
        "Test assertion, not URL validation: checks that the landing page's "
        "help copy mentions dash.cloudflare.com.",
    ),
    (
        "py/incomplete-url-substring-sanitization",
        "tests/src/unit/test_oauth.py",
        "may be at an arbitrary position",
        "Test assertion, not URL validation: checks that the consent HTML "
        "displays the redirect host to the user.",
    ),
    (
        "py/incomplete-url-substring-sanitization",
        "tests/src/unit/test_oauth_legacy_component.py",
        "may be at an arbitrary position",
        "Test assertion, not URL validation: the XSS-escape test checks the "
        "redirect host appears (escaped) in the response body.",
    ),
)


def _allowlist_reason(file: str, rule_id: str, message: str) -> str | None:
    """Return the allowlist reason if this finding is a verified suppression."""
    for rule, path, substr, reason in ALLOWLIST:
        if rule_id == rule and path in file and substr in message:
            return reason
    return None


def _rule_id(result: dict[str, Any], run: dict[str, Any]) -> str:
    """Resolve a result's rule id, falling back to the rules table by index."""
    if rule_id := result.get("ruleId"):
        return str(rule_id)
    rule_index = result.get("rule", {}).get("index")
    if rule_index is None:
        return "<unknown>"
    rules = run.get("tool", {}).get("driver", {}).get("rules", [])
    if 0 <= rule_index < len(rules):
        return str(rules[rule_index].get("id", "<unknown>"))
    return "<unknown>"


def _location(result: dict[str, Any]) -> tuple[str, int]:
    """Return (file, line) for a result's primary physical location."""
    locations = result.get("locations") or []
    if not locations:
        return ("<no-location>", 0)
    phys = locations[0].get("physicalLocation") or {}
    uri = (phys.get("artifactLocation") or {}).get("uri") or "<no-file>"
    line = (phys.get("region") or {}).get("startLine") or 0
    return (uri, line)


def classify(
    sarif_path: Path,
) -> tuple[list[tuple[str, int, str, str]], list[tuple[str, int, str, str, str]]]:
    """Return (gating_findings, suppressed) from a SARIF file.

    ``gating_findings`` are (file, line, rule_id, message) tuples the gate fails
    on. ``suppressed`` are (file, line, rule_id, message, reason) tuples dropped
    by the allowlist (reported, never silent). Findings under vendored
    ``PATHS_IGNORE`` prefixes are dropped entirely.
    """
    data = json.loads(sarif_path.read_text(encoding="utf-8"))
    findings: list[tuple[str, int, str, str]] = []
    suppressed: list[tuple[str, int, str, str, str]] = []
    for run in data.get("runs", []):
        for result in run.get("results", []):
            rule_id = _rule_id(result, run)
            file, line = _location(result)
            if any(file.startswith(prefix) for prefix in PATHS_IGNORE):
                continue
            message = ((result.get("message") or {}).get("text") or "").strip()
            if reason := _allowlist_reason(file, rule_id, message):
                suppressed.append((file, line, rule_id, message, reason))
                continue
            findings.append((file, line, rule_id, message))
    findings.sort(key=lambda f: (f[2], f[0], f[1]))
    suppressed.sort(key=lambda f: (f[2], f[0], f[1]))
    return findings, suppressed


def load_findings(sarif_path: Path) -> list[tuple[str, int, str, str]]:
    """Return the sorted list of gating (file, line, rule_id, message) findings."""
    return classify(sarif_path)[0]


def render(findings: list[tuple[str, int, str, str]]) -> str:
    """Render a human-readable, grouped report of the findings."""
    if not findings:
        return "No CodeQL code-quality findings. ✅"
    by_rule: dict[str, list[tuple[str, int, str, str]]] = defaultdict(list)
    for f in findings:
        by_rule[f[2]].append(f)
    counts = Counter(f[2] for f in findings)

    lines: list[str] = [f"CodeQL code-quality findings: {len(findings)}", ""]
    lines.append("Count by rule:")
    for rule, count in counts.most_common():
        lines.append(f"  {count:>4}  {rule}")
    lines.append("")
    for rule in sorted(by_rule):
        lines.append(f"## {rule} ({len(by_rule[rule])})")
        for file, line, _, message in by_rule[rule]:
            lines.append(f"  {file}:{line}  {message}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <quality.sarif>", file=sys.stderr)
        return 2
    sarif_path = Path(argv[1])
    if not sarif_path.exists():
        print(f"SARIF file not found: {sarif_path}", file=sys.stderr)
        return 2

    try:
        findings, suppressed = classify(sarif_path)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Could not read SARIF file {sarif_path}: {e}", file=sys.stderr)
        return 2
    report = render(findings)
    if suppressed:
        lines = [f"\nSuppressed (allowlisted false positives): {len(suppressed)}"]
        for file, line, rule_id, _msg, reason in suppressed:
            lines.append(f"  {file}:{line}  {rule_id} — {reason}")
        report += "\n".join(lines) + "\n"
    print(report)

    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(f"# CodeQL Code Quality\n\n```\n{report}\n```\n")

    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
