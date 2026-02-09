# ha-mcp Code Review Guidelines

## Test Coverage Requirements

**CRITICAL**: All PRs that modify source code in `src/` MUST include tests.

- If `src/` files are modified without corresponding test additions/updates, comment with HIGH severity
- Tests should be in `tests/` directory following naming convention: `test_<module>.py`
- E2E tests preferred for tool changes: `tests/src/e2e/`
- Unit tests for utilities: `tests/src/unit/`
- Exception: Documentation-only changes (`*.md` files)

## Security Patterns

Watch for these patterns and flag with HIGH severity:

1. **Unescaped user input** in f-strings or string interpolation
2. **`eval()` or `exec()` calls** - Never acceptable
3. **Credentials in code** - API keys, tokens, passwords
4. **SQL injection risks** - String concatenation in queries

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

## Return Value Format

All tools MUST return consistent format:

- Success: `{"success": True, "data": result}`
- Partial: `{"success": True, "partial": True, "warning": "..."}`
- Failure: `{"success": False, "error": {...}}`

Flag HIGH severity if tools return inconsistent structures.

## Home Assistant API Conventions

HA API uses SINGULAR field names:

- `trigger` not `triggers`
- `action` not `actions`
- `condition` not `conditions`

Flag MEDIUM severity if code uses plural field names for HA API calls.

## Code Conventions

1. **Tool descriptions**: Use action verbs, keep concise, reference `ha_get_domain_docs()` for complex schemas
2. **Async/await**: Use consistently for I/O operations
3. **Type hints**: Required for all function signatures
4. **Docstrings**: One-line summary starting with action verb

## Documentation Standards

1. **Docstrings**: One-line summary starting with action verb
2. **Comments**: Only for non-obvious logic
3. **CHANGELOG.md**: Auto-generated via semantic-release (don't edit manually)

## Architecture Alignment

1. **New tools**: Create `tools_<domain>.py` with `register_<domain>_tools()` function
2. **Shared logic**: Use service layer (`smart_search.py`, `device_control.py`)
3. **WebSocket operations**: Verify state changes in real-time
4. **Tool completion**: Operations should wait for completion (not just API acknowledgment)

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
