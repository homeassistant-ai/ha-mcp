# ha-mcp Code Review Guidelines

## Project Context

**ha-mcp** is a Model Context Protocol (MCP) server that enables AI assistants to control Home Assistant smart homes. It provides 80+ tools for entity control, automations, device management, and configuration via Home Assistant's REST and WebSocket APIs.

**Key Technologies:**
- Python 3.13, FastMCP framework
- Home Assistant REST API & WebSocket API
- MCP Protocol (Model Context Protocol)
- Architecture: Tool registry with lazy loading, service layer pattern, WebSocket state verification

**Code Organization:**
- `src/ha_mcp/tools/` - 80+ MCP tools (tool modules auto-discovered)
- `src/ha_mcp/client/` - REST and WebSocket clients
- `tests/src/e2e/` - End-to-end tests with real Home Assistant instance
- `tests/src/unit/` - Unit tests for utilities

## Test Coverage Requirements

Use best judgement - not all changes require new tests, but the overall feature/tool should be tested.

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

Flag HIGH severity if annotation contradicts actual behavior in the implementation.

## Tool Naming Convention

All MCP tools MUST follow `ha_<verb>_<noun>` pattern:

- `ha_get_*` — single item retrieval
- `ha_list_*` — collections
- `ha_search_*` — filtered queries
- `ha_set_*` — create/update operations
- `ha_delete_*` — remove operations
- `ha_call_*` — execute operations

Flag MEDIUM severity if tools don't follow this pattern.

## Tool File Organization

New tools MUST be in `tools_<domain>.py` with `register_<domain>_tools()` function. Tools are auto-discovered by registry - no manual registration needed.

## Structured Error Responses

All error handling MUST use:

```python
from ..errors import create_error_response, ErrorCode
return create_error_response(
    code=ErrorCode.APPROPRIATE_CODE,
    message="Clear error description",
    suggestions=["Actionable suggestion"]
)
```

Flag HIGH severity if errors use plain exceptions or dict returns instead of structured errors from `errors.py`.


## Code Conventions

1. **Tool descriptions**: Use action verbs, keep concise
2. **Async/await**: Use consistently for I/O operations
3. **Type hints**: Required for all function signatures

## Documentation Standards

1. **Comments**: Only for non-obvious logic - too many comments is an anti-pattern (code should be self-documenting)
2. **CHANGELOG.md**: Auto-generated via semantic-release (don't edit manually)

## Architecture Alignment

1. **New tools**: Create `tools_<domain>.py` with `register_<domain>_tools()` function
2. **Shared logic**: Use service layer (`smart_search.py`, `device_control.py`)
3. **WebSocket operations**: Verify state changes in real-time
4. **Tool completion**: Operations should wait for completion (not just API acknowledgment)

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
