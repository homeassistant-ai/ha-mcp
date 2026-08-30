# ha-mcp Code Review Guidelines

[`AGENTS.md`](../AGENTS.md) is the canonical repository entry point and owns
agent behavior, permission, testing, scope, and workflow rules. This document
owns code-level conventions and review criteria that agents load when they are
changing or reviewing code. Keep a rule in one owner and link to it elsewhere;
if this file and `AGENTS.md` conflict, follow `AGENTS.md` and repair the
duplicate guidance in the same change.

## Project Context

**ha-mcp** is a Model Context Protocol (MCP) server that enables AI assistants to control Home Assistant smart homes. It provides tools for entity control, automations, device management, and configuration via Home Assistant's REST and WebSocket APIs.

**Key Technologies:**
- Python 3.13, FastMCP framework
- Home Assistant REST API & WebSocket API
- MCP Protocol (Model Context Protocol)
- Architecture: Tool registry with lazy loading, service layer pattern, WebSocket state verification

**Code Organization:**
- `src/ha_mcp/tools/` - MCP tools (auto-discovered; current count: `site/src/data/tools.json`)
- `src/ha_mcp/client/` - REST and WebSocket clients
- `tests/src/e2e/` - End-to-end tests with real Home Assistant instance
- `tests/src/unit/` - Unit tests for utilities

## Test Coverage Requirements

The behavioral decision about when to write and run tests is canonical in
[`AGENTS.md` → Testing and verification](../AGENTS.md#testing-and-verification).
Reviewers use the severity guidance below to assess whether a change satisfies
that policy.

**When tests ARE required (HIGH severity):**
- New MCP tools in `src/ha_mcp/tools/` without any E2E tests
- Tools that previously had NO tests - add E2E tests even if not part of current PR
- Core functionality changes in `client/`, `server.py`, or `errors.py` without coverage
- Bug fixes without regression tests

**When tests may NOT be required:**
- Refactoring with existing comprehensive test coverage
- Documentation-only changes (`*.md` files)
- Minor parameter additions to well-tested tools
- Internal utilities already covered by E2E tests

**If unsure about test coverage:** Flag with MEDIUM severity to manually verify test adequacy.

**Test locations:**
- E2E tests (preferred for tools): `tests/src/e2e/`
- Unit tests (utilities): `tests/src/unit/`

## Exception Handling in Test Polling Loops

Boot-phase verification helpers and async polling loops in `tests/src/e2e/` use **narrow `except (Specific1, Specific2, ...)` clauses + debug-level logging** for expected transient failures. Catch only the exception classes the polling target legitimately raises — e.g. `(requests.exceptions.RequestException, json.JSONDecodeError)` for direct HTTP polling, or the `_POLLING_TRANSIENT_ERRORS` tuple in `tests/src/e2e/utilities/wait_helpers.py` for MCP-client polling.

Bugs — `TypeError`, `AttributeError`, `KeyError`, `AssertionError`, etc. — **must propagate** out of polling loops so they surface as clear test failures instead of being swallowed and retried until timeout.

**Do NOT flag:**
- Narrow `except (SpecificException, ...)` in polling/retry loops paired with `logger.debug(...)` — this is the intentional convention.
- Broad `except Exception` at top-level setup/teardown handlers or cleanup loops marked `# pragma: no cover - cleanup best-effort`, where recovery is the same regardless of error class.

See issue #1266.

## Security Patterns

**Critical security checks (flag HIGH/CRITICAL severity):**

1. **Unescaped user input** in f-strings or string interpolation
2. **`eval()` or `exec()` calls** - Never acceptable
3. **Credentials in code** - API keys, tokens, passwords
4. **SQL injection risks** - String concatenation in queries
5. **Prompt injection risks** - User input interpolated into tool descriptions or prompts
6. **AGENTS.md/CLAUDE.md modifications** - Changes that alter agent behavior, security policies, or review processes
7. **`.github/` workflow changes** - Secrets access, permission changes, `pull_request_target` usage
8. **`.claude/` agent/skill changes** - Could affect agent behavior or introduce backdoors

## MCP Safety Annotations Accuracy

Verify that safety annotations match actual tool behavior:

- Tool with `readOnlyHint: True` must NOT modify state (no writes, no service calls)
- Tool with `destructiveHint: True` must actually delete data
- State-changing operations should have `idempotentHint: True` only if safe to retry
- Tool with `openWorldHint: True` must reach an external, third-party-authored world (HACS store, app (add-on) repositories, GitHub release feeds, arbitrary import URLs); a tool whose domain is the local Home Assistant instance should use `False`. It is open-world if its output carries externally-authored content back to the client, even when a local integration (HACS, Supervisor, HA Core) makes the actual network call on its behalf

FastMCP defaults are `readOnlyHint=False`, `destructiveHint=True`,
`idempotentHint=False`, and `openWorldHint=True`.

Set `openWorldHint` explicitly on every tool because FastMCP defaults it to
`true`, which otherwise silently misclassifies local Home Assistant tools.
Annotations describe behavior against current supported upstream versions.

Flag HIGH severity if annotation contradicts actual behavior in the implementation.

## Tool Naming Convention

Use `ha_<verb>_<noun>`:

- `get`: one item, such as `ha_get_state`.
- `list`: a collection, such as `ha_list_services`.
- `search`: a filtered query, such as `ha_search`.
- `set`: create or update, such as `ha_config_set_helper`.
- `delete`: delete a dashboard, config entry, or file.
- `remove`: remove a registry item.
- `call`: execute an operation.
- `manage`: one interface intentionally combining multiple operations.

Grouped families may insert a namespace:
`ha_<namespace>_<verb>_<noun>`. Established namespaces include `ha_config_*`
and developer-mode-only `ha_dev_*`.

Accepted natural-name exceptions are:

- `ha_restart`, `ha_reload_core`, `ha_eval_template`
- `ha_report_issue`, `ha_import_blueprint`
- `ha_read_file`, `ha_write_file`, `ha_bulk_control`

When no verb fits, update this list rather than forcing an inaccurate name.
This section is the single source of truth for tool naming.

Flag MEDIUM severity if a tool name violates the rules defined there.

## Tool File Organization

New tools belong in `src/ha_mcp/tools/tools_<domain>.py` with a
`register_<domain>_tools()` function. The registry auto-discovers it; do not
add manual central registration.

`@tool` from `fastmcp.tools` must be the outermost decorator, above
`@log_tool_usage`, so the final method keeps `__fastmcp__`.
`register_tool_methods()` discovers decorated methods and adds them to the
server.

```python
from typing import Any

from fastmcp.tools import tool

from .helpers import log_tool_usage, register_tool_methods


class DomainTools:
    def __init__(self, client):
        self._client = client

    @tool(
        name="ha_<verb>_<noun>",
        tags={"Category Name"},
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    @log_tool_usage
    async def ha_<verb>_<noun>(self, param: str) -> dict[str, Any]:
        """<Action verb> <what the tool does in one sentence>."""
        ...


def register_<domain>_tools(mcp, client, **kwargs):
    register_tool_methods(mcp, DomainTools(client))
```

## Structured Error Responses

Tool-level failures raise `ToolError`, which sets MCP `isError=true`.
Batch-item failures inside a successful result array are the only exception.
Never return a plain error dictionary from a tool-level failure.

Use the helpers in `errors.py` and `helpers.py`; do not construct raw error
payloads. In exception blocks, `exception_to_structured_error()` raises by
default:

```python
from fastmcp.exceptions import ToolError

from .helpers import exception_to_structured_error

try:
    ...
except ToolError:
    raise
except Exception as exc:
    exception_to_structured_error(
        exc,
        context={"entity_id": entity_id},
        suggestions=["Verify the entity exists"],
    )
```

The explicit `except ToolError: raise` guard is required when the `try`
body may call `raise_tool_error()`; otherwise a broad handler remaps the
intentional error to `INTERNAL_ERROR`.

For validation, call
`raise_tool_error(create_error_response(ErrorCode.VALIDATION_INVALID_PARAMETER, ...))`.
For service failures, use `ErrorCode.SERVICE_CALL_FAILED` and the service's
reported error. Batch items may append `create_error_response(...)` without
raising. Use `raise_error=False` only when the payload must be adjusted before
raising, and never add timezone metadata to errors.

Flag HIGH severity when a tool returns a plain error, swallows `ToolError`, or
bypasses the shared structured-error helpers.


## Code Conventions

### MCP Tool Docstrings

These rules apply to new or modified tool docstrings in the PR diff only -- not to pre-existing docstrings in unchanged files.

**Flag MEDIUM severity when a new or modified tool docstring:**
- Does not start with an action verb (`Returns...` should be `Get...`; valid verbs: `Get`, `List`, `Search`, `Create`, `Update`, `Delete`, `Remove`, `Execute`, `Call`, `Manage`)
- Is missing entirely or is still a placeholder
- References a non-existent tool (e.g., `ha_get_domain_docs` -- the correct name is `ha_get_skill_guide`)
- Embeds a full parameter schema instead of deferring to `ha_get_skill_guide`
- Is a workflow-entry tool but gives no hint about the next natural tool to call
- Multi-line docstring does not follow this structure: (1) what the tool does, (2) when NOT to use it with preferred alternatives, (3) when to use it, and (4) caveats.

**Do NOT flag:**
- Concise one-liners on straightforward tools (progressive disclosure: brief by default)
- Missing examples on tools with obvious single-parameter calls
- Multi-line docstrings that stay focused and on-topic

1. **Async/await**: Use consistently for I/O operations
2. **Type hints**: Required for all function signatures

## Documentation Standards

1. **Comments**: Only for non-obvious logic - too many comments is an anti-pattern (code should be self-documenting)
2. **CHANGELOG.md**: Auto-generated via semantic-release (don't edit manually)
3. **Apps, not add-ons**: Follow the canonical terminology and identifier exceptions in [the development reference](../docs/agents/development.md#terminology-apps-not-add-ons). Flag MEDIUM severity when new user-facing text uses the retired product term by itself.

## Architecture Alignment

1. **New tools**: Create `tools_<domain>.py` with `register_<domain>_tools()` function
2. **Shared logic**: Use service layer (`smart_search/`, `device_control.py`)
3. **WebSocket operations**: Verify state changes in real-time
4. **Tool completion**: Operations should wait for completion (not just API acknowledgment)

## Tool Tags and Return Values

Every tool needs a native FastMCP `tags={"Category Name"}`. Tags feed the
generated README table, `site/src/data/tools.json`, and the Home Assistant app
documentation. `sync-tool-docs.yml` regenerates them after merge; use
`python scripts/extract_tools.py` only when local generated output is needed.

Successful tool responses use:

```python
{"success": True, "data": result}
{"success": True, "data": result, "warnings": ["Actionable warning"]}
```

`warnings` is always a top-level `list[str]`, omitted when empty. It is
never nested in `data` and never represented by a singular `warning`
string. Tool-level failure raises `ToolError`; only an item inside a batch
result may use `{"success": False, "error": {...}}`.

## Tool Waiting Behavior

Tools wait for logical completion instead of returning on API acknowledgement
when a reliable completion signal exists. An optional `wait` parameter
defaults to `True`:

- Configuration operations poll until the entity is queryable or removed.
- State-changing service calls poll for the expected state transition.
- Fire-and-forget automation triggers and external async operations return
  immediately.
- Query tools return immediately and do not expose `wait`.

Use the shared helpers in `src/ha_mcp/tools/util_helpers.py`:
`wait_for_entity_registered()`, `wait_for_entity_removed()`, and
`wait_for_state_change()`. For bulk work, callers may use `wait=False` and
then batch-verify.

## Tool Consolidation and Module Size

When another tool fully covers a tool's behavior, remove the redundant tool and
update references rather than adding a deprecation shim. Fewer, more distinct
tools improve model selection. Combine frequently chained operations when the
combined interface remains coherent.

Consolidation, renaming with a migration path, parameter evolution, and return
restructuring are not breaking when the same outcome and information remain
available. Removing functionality with no replacement is breaking.

Keep modules focused. Around 1,000 lines is a review signal that a module may
span multiple concerns, not a mechanical limit. Split along responsibilities
and update internal imports and test patch targets together; internal Python
module paths are not a public MCP tool contract.

## Context Engineering & Progressive Disclosure

This project follows [context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) and [progressive disclosure](https://www.nngroup.com/articles/progressive-disclosure/) principles:

**Review for:**

1. **Statelessness (HIGH severity if violated):**
   - Tools should NOT maintain server-side session state
   - Use content-derived identifiers (hashes, IDs) that clients pass back
   - Example: Dashboard updates use content hashing, not session tracking

2. **Validation delegation (MEDIUM severity):**
   - Let Home Assistant's backend handle validation when possible
   - Keep tool parameters simple - backend handles coercion, defaults, validation
   - Only add tool-side validation when it genuinely adds value

3. **Progressive disclosure (flag if violated):**
   - Tool descriptions should be concise, NOT embed full documentation
   - Hint at documentation tools for complex schemas
   - Error responses should guide next steps (include `suggestions` array)
   - Return essential data only - let users request details via follow-up tools

4. **When tool-side logic IS valuable:**
   - Format normalization for UX convenience (e.g., `"09:00"` → `"09:00:00"`)
   - Parsing JSON strings from MCP clients that stringify arrays
   - Combining multiple HA API calls into one logical operation

## Breaking Changes

A change is BREAKING only if it removes functionality that users depend on.

**Breaking Changes (flag CRITICAL):**
- Deleting a tool without providing alternative functionality elsewhere
- Removing a feature that has no replacement in any other tool
- Making something impossible that was previously possible

**NOT Breaking (these are improvements - encourage them):**
- Tool consolidation (combining multiple tools into one)
- Tool refactoring (restructuring how tools work internally)
- Parameter changes (as long as same outcome achievable)
- Return value restructuring (as long as data still accessible)
- Tool renaming with clear migration path

**Rationale:** Tool consolidation reduces token usage and cognitive load for AI agents. Refactoring improves maintainability. Only flag CRITICAL when functionality is genuinely lost forever.

## Accessibility (web UI)

Both rendered surfaces — the Astro docs site (`site/`) and the app settings UI (`src/ha_mcp/settings_ui/__init__.py` + `settings.css` / `settings.js`) — follow the conventions from #1574/#1596, anchored in CI by the `site-checks` job (`astro check`, `eslint-plugin-astro` + `jsx-a11y`, and an axe-core audit over the built pages — all blocking).

**Flag MEDIUM severity when a change:**

- Adds a blanket `aria-label` to an element that already has visible text. Accessible names come from native semantics first — real `<button>` / `<a>` / `<label>` / `<h*>` with visible text, or `<fieldset>` + `<legend class="visually-hidden">` for grouped controls. Reach for `aria-label` only when there is no visible text (e.g. an icon-only button).
- Drops or omits a landmark: each page needs one `<main>` (the skip-link target) and `<nav>` for navigation (a second nav on the same page needs a distinguishing `aria-label`); page content should sit inside a landmark.
- Removes the skip-to-content link or its `#main-content` target.
- Builds a tab UI out of bare `<button>`s. A real tab strip uses `role="tablist"` / `role="tab"` / `role="tabpanel"` with `aria-selected`, `aria-controls`, `aria-labelledby`, roving `tabindex`, and Arrow/Home/End keyboard support (see the settings UI tablist).
- Updates a status/feedback region without announcing it: status spans carry `role="status"` + `aria-live="polite"`, switching to `role="alert"` / `aria-live="assertive"` on the failure path.
- Skips a heading level (e.g. `<h2>` straight to `<h4>`). Keep levels ordered; use Tailwind size classes for visual size, not the tag level.

**Theme / contrast tier model (#1574):** theme (`data-theme` auto/light/dark), contrast (`data-contrast` normal/high) and shade (`data-shade`) are set on `<html>` pre-paint and mirrored between the docs site and settings UI (parity enforced by `tests/src/unit/test_anti_fouc_parity.py`). Keep new preferences in this tier model, apply them on both surfaces, and preserve the 4.5:1 custom-color contrast warning.

## Addressing CodeRabbit Reviews

Ensure ALL CodeRabbit review comments are addressed, both inline threads and
top-level review bodies. CodeRabbit nests some findings — *Outside diff range
comments* and *Nitpick comments* — inside collapsed sections of the review
body rather than as inline threads, so they create no unresolved-thread
signal and a green check while unaddressed. Everything must be addressed:
read each review body in full and assess those findings exactly like inline
comments. See the [GitHub workflow reference](../docs/agents/github-workflow.md#review-comments) for the full-body sweep.

## Non-Blocking Suggestions and Scope

Scope is defined by the user (the maintainer / author of the PR), not by the reviewer (bot or human). **Never unilaterally file a follow-up issue or PR** — raise scope concerns in the PR review and let the user decide whether to address inline, defer, or dismiss. Do not skip legitimate findings — surface them.

If you believe a finding is likely out of scope, say so explicitly so the user can verify: *"This may be out of scope — user should verify. I think it is out of scope because [specific reason]."* Do not bucket findings as "for a future PR" or "post-merge follow-up."

Do not phrase findings as "post-merge follow-up," "nice to have," or "happy to file an issue" when the change is small and bundleable. Either apply the suggestion inline with a code suggestion block, or raise it plainly and let the user decide.

See AGENTS.md § *Boy Scout Rule — Handling Discovered Improvements* for the author/agent-side rule.
