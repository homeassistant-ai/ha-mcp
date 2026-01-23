# AGENTS.md

## Quick Reference

**Home Assistant MCP Server** — MCP server enabling AI assistants to control Home Assistant smart homes (80+ tools).

| | |
|-|-|
| Python | 3.13 only |
| Package manager | `uv` |
| Repo | `homeassistant-ai/ha-mcp` |
| PyPI | `ha-mcp` |

### Commands
```bash
uv sync --group dev                          # Install with dev deps
uv run ruff check src/ tests/ --fix          # Lint
uv run mypy src/                             # Type check
uv run pytest tests/src/e2e/ -v --tb=short   # E2E tests (requires Docker)
```

### Core Rules
- **Never commit directly to master** — always create feature/fix branches
- **Never push or create PRs without asking user first**
- Tests are in `tests/src/e2e/` (not `tests/e2e/`)
- Test token centralized in `tests/test_constants.py`

### External Resources
| Resource | URL | Use For |
|----------|-----|---------|
| HA REST API | https://developers.home-assistant.io/docs/api/rest | Entity states, services |
| HA WebSocket API | https://developers.home-assistant.io/docs/api/websocket | Real-time events |
| HA Core Source | `gh api /search/code -f q="... repo:home-assistant/core"` | Undocumented APIs |
| FastMCP Docs | https://gofastmcp.com/getting-started/welcome | MCP framework |
| MCP Spec | https://modelcontextprotocol.io/docs | Protocol details |

---

## Architecture

```
src/ha_mcp/
├── server.py              # FastMCP server
├── config.py              # Pydantic settings
├── errors.py              # 38 structured error codes
├── client/
│   ├── rest_client.py     # HTTP REST API
│   └── websocket_client.py
├── tools/                 # 28 modules, 80+ tools
│   ├── registry.py        # Lazy auto-discovery (finds tools_*.py)
│   └── tools_*.py         # Domain-specific tools
└── utils/
    └── fuzzy_search.py    # textdistance-based matching
```

**Key patterns:**
- **Auto-discovery**: Registry finds `tools_*.py` with `register_*_tools()` — no manual registration
- **Lazy init**: Server, client, tools created on-demand
- **WebSocket verification**: Device ops verified via real-time state changes

---

## Git & PR Workflow

### Branching
```bash
git checkout -b feature/description   # or fix/, improve/
git add . && git commit -m "feat: description"
# ASK USER before pushing
```

### PR Workflow
After creating/updating a PR:

1. Push changes
2. Wait for CI:
   ```bash
   gh pr checks <PR> --watch   # Polls until complete
   ```
   Or if doing other work: `sleep 60` then check periodically.
3. If failures, view logs and fix:
   ```bash
   gh run view <run-id> --log-failed
   ```
4. Check for review comments:
   ```bash
   gh pr view <PR> --json comments
   gh api repos/homeassistant-ai/ha-mcp/pulls/<PR>/comments
   ```
5. Address comments (prioritize human over bot comments)
6. Repeat until all checks green and comments addressed

### PR Execution Philosophy
- Work autonomously — don't ask about every small decision
- Make reasonable technical decisions based on codebase patterns
- Fix unrelated test failures encountered during CI
- **DO NOT** choose based on what's faster to implement
- **DO** consider long-term codebase health — refactoring that benefits maintainability is valid
- **For non-obvious choices**: create 2 PRs with different approaches, let user choose
- **Final report**: summarize choices made, problems encountered, suggested improvements

### Boy Scout Rule
"Leave code cleaner than you found it" — but scoped correctly:
- **Applies to**: code you're already modifying as part of your task
- **Does NOT mean**: refactor unrelated code you happen to see

| Scenario | Action |
|----------|--------|
| No tests for code you're changing | Add tests for your changes only |
| Low test coverage in your area | Add tests for gaps |
| Poor test quality | Improve if straightforward |
| Major code quality issues | Open an issue, don't fix inline |

### Improvements Outside Current Scope
When you identify improvements unrelated to your current task:
- Create a **separate PR** (branch from master)
- Never mix improvements with main feature PR
- Mention improvement PRs in your final summary

### Hotfix Process
**Only for critical bugs in current stable release.**

```bash
# Verify bug exists in stable first
git fetch --tags --force
git show stable:path/to/file.py | grep "buggy_code"

# If exists in stable:
git checkout -b hotfix/description stable
# fix, commit
gh pr create --base master
```

If code doesn't exist in stable, use regular `fix/` branch from master.

---

## Writing MCP Tools

### Naming Convention
`ha_<verb>_<noun>`:
- `get` — single item (`ha_get_state`)
- `list` — collections (`ha_list_areas`)
- `search` — filtered queries (`ha_search_entities`)
- `set` — create/update (`ha_config_set_helper`)
- `delete` — remove (`ha_config_delete_automation`)
- `call` — execute (`ha_call_service`)

### Tool Structure
Create `tools_<domain>.py` in `src/ha_mcp/tools/`. Registry auto-discovers it.

```python
from typing import Any

def register_<domain>_tools(mcp, client, **kwargs):
    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    @log_tool_usage
    async def ha_<verb>_<noun>(param: str) -> dict[str, Any]:
        """One-line summary starting with action verb."""
        # For complex schemas, add: "Use ha_get_domain_docs('<domain>') for details."
```

### Safety Annotations
| Annotation | Use For |
|------------|---------|
| `readOnlyHint: True` | No side effects |
| `idempotentHint: True` | Safe to retry |
| `destructiveHint: True` | Deletes data |

### Error Handling
Use structured errors from `errors.py`:
```python
from ..errors import create_error_response, ErrorCode
return create_error_response(
    code=ErrorCode.ENTITY_NOT_FOUND,
    message="Entity not found",
    suggestions=["Use ha_search_entities() to find valid IDs"]
)
```

### Return Values
```python
{"success": True, "data": result}                    # Success
{"success": True, "partial": True, "warning": "..."}  # Degraded
{"success": False, "error": {...}}                    # Failure
```

---

## Issue Triage

### Labels
| Label | Meaning |
|-------|---------|
| `ready-to-implement` | Clear path, no decisions needed |
| `needs-choice` | Multiple approaches, needs input |
| `needs-info` | Awaiting clarification |
| `triaged` | Analysis complete |

### Triage Workflow
When asked to "triage issues":
1. List untriaged: `gh issue list --state open --json number,title,labels --jq '.[] | select(.labels | map(.name) | contains(["triaged"]) | not)'`
2. Launch parallel triage agents (one Task per issue, all in same message)
3. Each agent: analyzes issue, explores code, updates labels, posts comment

---

## CI/CD & Release

### Workflows
| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `pr.yml` | PR opened | Lint, type check |
| `e2e-tests.yml` | PR to master | Full E2E tests |
| `publish-dev.yml` | Push to master | Dev release `.devN` |
| `semver-release.yml` | Weekly Tue 10:00 UTC | Stable release |
| `hotfix-release.yml` | Hotfix PR merged | Immediate patch |

### Commit Conventions
| Prefix | Bump | Changelog |
|--------|------|-----------|
| `fix:`, `perf:`, `refactor:` | Patch | User-facing |
| `feat:` | Minor | User-facing |
| `feat!:` or `BREAKING CHANGE:` | Major | User-facing |
| `docs:` | None | User-facing |
| `chore:`, `ci:`, `test:` | None | Internal |
| `*:(internal)` | Same as type | Internal only |

---

## Home Assistant Add-on

**Required files:** `repository.yaml` (root), `homeassistant-addon/config.yaml` (must match `pyproject.toml` version)

**Docs**: https://developers.home-assistant.io/docs/add-ons

---

## API Research

Search HA Core without cloning (500MB+ repo):
```bash
# Search for patterns
gh search code "use_blueprint" --repo home-assistant/core path:tests --json path --limit 10

# Fetch file contents (base64 encoded)
gh api /repos/home-assistant/core/contents/homeassistant/components/automation/config.py \
  --jq '.content' | base64 -d > /tmp/ha_config.py
```

---

## Test Patterns

**FastMCP validates required params at schema level.** Don't test for missing required params:
```python
# BAD: Fails at schema validation
await mcp.call_tool("ha_config_get_script", {})

# GOOD: Test with valid params but invalid data
await mcp.call_tool("ha_config_get_script", {"script_id": "nonexistent"})
```

**HA API uses singular field names:** `trigger` not `triggers`, `action` not `actions`.

---

## Custom Agents

Located in `.claude/agents/`:

| Agent | Purpose |
|-------|---------|
| `triage` | Triage issues, assess complexity, update labels |
| `issue-to-pr-resolver` | End-to-end: issue → branch → implement → PR → CI green |
| `pr-checker` | Review PR comments, resolve threads, monitor CI |

---

## Context Engineering

### Principles
- **Stateless over stateful** — use content-derived IDs (hashes) instead of session state
- **Delegate validation** — let HA backend validate, it has better error messages
- **Docs on demand** — reference `ha_get_domain_docs()` instead of embedding docs in tool descriptions
- **Minimal returns** — return essential data, let user request details via follow-up tools

### When Tool-Side Logic Adds Value
- Format normalization (`"09:00"` → `"09:00:00"`)
- Parsing JSON strings from MCP clients that stringify arrays
- Combining multiple HA API calls into one operation

### Progressive Disclosure in Practice
- Tool descriptions hint at related tools (`ha_search_entities` suggests `ha_get_state`)
- Error responses include `suggestions` array for next steps
- Required params first, optional with sensible defaults
