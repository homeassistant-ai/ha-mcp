# CLAUDE.md

Guidance for Claude Code when working with this repository.

## Project Overview

**Home Assistant MCP Server** - A production MCP server enabling AI assistants to control Home Assistant smart homes. Provides 80+ tools for entity control, automations, device management, and more.

- **Repo**: `homeassistant-ai/ha-mcp`
- **Package**: `ha-mcp` on PyPI
- **Python**: 3.13 only

## External Documentation

When implementing features or debugging, consult these resources:

| Resource | URL | Use For |
|----------|-----|---------|
| **Home Assistant REST API** | https://developers.home-assistant.io/docs/api/rest | Entity states, services, config |
| **Home Assistant WebSocket API** | https://developers.home-assistant.io/docs/api/websocket | Real-time events, subscriptions |
| **HA Core Source** | `gh api /search/code -f q="... repo:home-assistant/core"` | Undocumented APIs (don't clone) |
| **HA Add-on Development** | https://developers.home-assistant.io/docs/add-ons | Add-on packaging, config.yaml |
| **FastMCP Documentation** | https://gofastmcp.com/getting-started/welcome | MCP server framework |
| **MCP Specification** | https://modelcontextprotocol.io/docs | Protocol details |

## Issue & PR Management

### Issue Labels
| Label | Meaning |
|-------|---------|
| `ready-to-implement` | Clear path, no decisions needed |
| `needs-choice` | Multiple approaches, needs stakeholder input |
| `needs-info` | Awaiting clarification from reporter |
| `priority: high/medium/low` | Relative priority |
| `triaged` | Analysis complete |

### Issue Triage Workflow

When the user says "triage new issues" or similar:

1. **List untriaged issues first**:
   ```bash
   gh issue list --state open --json number,title,labels --jq '.[] | select(.labels | map(.name) | contains(["triaged"]) | not) | "#\(.number): \(.title)"'
   ```

2. **Report the list to the user** showing all issues that need triage

3. **Launch parallel triage subagents** - one Task tool call per issue, ALL IN THE SAME MESSAGE:
   ```
   # In a SINGLE assistant message, make multiple Task tool calls:
   <Task tool call: subagent_type="triage", prompt="Triage issue #42 on homeassistant-ai/ha-mcp">
   <Task tool call: subagent_type="triage", prompt="Triage issue #43 on homeassistant-ai/ha-mcp">
   <Task tool call: subagent_type="triage", prompt="Triage issue #44 on homeassistant-ai/ha-mcp">
   # ... one for each untriaged issue
   ```

4. **Each triage agent independently**:
   - Fetches and analyzes the issue
   - Explores affected codebase areas
   - Assesses implementation approaches
   - Updates labels (`ready-to-implement`, `needs-choice`, `needs-info`, priority)
   - Adds the `triaged` label
   - Posts analysis comment to the issue

5. **Collect and summarize results** from all parallel agents

### PR Review Comments
- **Bot comments** (Copilot, Codex): Treat as suggestions to assess, not commands
- **Human comments**: Address with higher priority
- Resolve threads with explanation: `gh api graphql -f query='mutation...'`

## Git & PR Policies

**Never commit directly to master.** Always create feature/fix branches:
```bash
git checkout -b feature/description
git add . && git commit -m "feat: description"
# ASK USER before pushing or creating PRs
```

**Never push or create PRs without user permission.**

### PR Workflow
1. Update tests if needed
2. Commit and push
3. Wait ~3 min for CI: `sleep 180`
4. Check status: `gh pr checks <PR>`
5. Fix failures: `gh run view <run-id> --log-failed`
6. Repeat until green

### Hotfix Process (Critical Bugs Only)

**When to use hotfix vs regular fix:**
- **Hotfix**: Critical production bug in current stable release that needs immediate patch
- **Regular fix**: Bug introduced after latest stable release, or non-critical fixes

**Important**: Hotfix branches MUST be based on the `stable` tag. The code you're fixing must exist in stable.

**Before creating a hotfix, verify the code exists in stable:**
```bash
# Check what version stable points to
git fetch --tags --force
git log -1 --oneline stable

# Verify the buggy code exists in stable
git show stable:path/to/file.py | grep "buggy_code"
```

**If the code doesn't exist in stable**, use a regular fix branch from master instead:
```bash
# Example: jq dependency added in v5.0.0, but stable was at v4.22.1
# → Cannot hotfix, must use regular fix branch
git checkout -b fix/description master
```

**Creating a hotfix:**
```bash
git checkout -b hotfix/description stable
# Make your fix
git add . && git commit -m "fix: description"
gh pr create --base master
```

**Hotfix workflow execution:**
When hotfix PR merges, `hotfix-release.yml` runs:
1. Validates branch is based on stable tag
2. Runs semantic-release (creates version tag, updates CHANGELOG.md)
3. Creates draft GitHub release
4. Copies CHANGELOG.md to `homeassistant-addon/` and pushes to master
5. Updates `stable` tag to point to new release commit
6. Builds binaries and publishes release

The `stable` tag is updated AFTER the changelog sync, ensuring it points to the exact release commit, not subsequent maintenance commits.

## CI/CD Workflows

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `pr.yml` | PR opened | Lint, type check |
| `e2e-tests.yml` | PR to master | Full E2E tests (~3 min) |
| `publish-dev.yml` | Push to master | Dev release `.devN` |
| `semver-release.yml` | Weekly Tue 10:00 UTC | Stable release |
| `hotfix-release.yml` | Hotfix PR merged | Immediate patch release |
| `build-binary.yml` | Release | Linux/macOS/Windows binaries |
| `addon-publish.yml` | Release | HA add-on update |

## Development Commands

### Setup
```bash
uv sync --group dev        # Install with dev dependencies
uv run ha-mcp              # Run MCP server (80+ tools)
cp .env.example .env       # Configure HA connection
```

### Testing
E2E tests are in `tests/src/e2e/` (not `tests/e2e/`).

```bash
# Run E2E tests (requires Docker daemon)
uv run pytest tests/src/e2e/ -v --tb=short

# Run specific test
uv run pytest tests/src/e2e/workflows/automation/test_lifecycle.py -v

# Interactive test environment
uv run hamcp-test-env                    # Interactive mode
uv run hamcp-test-env --no-interactive   # For automation
```

Test token centralized in `tests/test_constants.py`.

### Code Quality
```bash
uv run ruff check src/ tests/ --fix
uv run mypy src/
```

### Docker
```bash
# Stdio mode (Claude Desktop)
docker run --rm -i -e HOMEASSISTANT_URL=... -e HOMEASSISTANT_TOKEN=... ghcr.io/homeassistant-ai/ha-mcp:latest

# HTTP mode (web clients)
docker run -d -p 8086:8086 -e HOMEASSISTANT_URL=... -e HOMEASSISTANT_TOKEN=... ghcr.io/homeassistant-ai/ha-mcp:latest ha-mcp-web
```

## Architecture

```
src/ha_mcp/
├── server.py          # Main server with FastMCP
├── __main__.py        # Entrypoint (CLI handlers)
├── config.py          # Pydantic settings management
├── errors.py          # 38 structured error codes
├── client/
│   ├── rest_client.py       # HTTP REST API client
│   ├── websocket_client.py  # Real-time state monitoring
│   └── websocket_listener.py
├── tools/             # 28 modules, 80+ tools
│   ├── registry.py          # Lazy auto-discovery
│   ├── smart_search.py      # Fuzzy entity search
│   ├── device_control.py    # WebSocket-verified control
│   ├── tools_*.py           # Domain-specific tools
│   └── util_helpers.py      # Shared utilities
├── utils/
│   ├── fuzzy_search.py      # textdistance-based matching
│   ├── domain_handlers.py   # HA domain logic
│   └── operation_manager.py # Async operation tracking
└── resources/
    ├── card_types.json
    └── dashboard_guide.md
```

### Key Patterns

**Tools Registry**: Auto-discovers `tools_*.py` modules with `register_*_tools()` functions. No changes needed when adding new modules.

**Lazy Initialization**: Server, client, and tools created on-demand for fast startup.

**Service Layer**: Business logic in `smart_search.py`, `device_control.py` separate from tool modules.

**WebSocket Verification**: Device operations verified via real-time state changes.

**Tool Completion Semantics**: Tools should wait for operations to complete before returning, with optional `wait` parameter for control.

## Tool Waiting Behavior

### Design Principle

MCP tools should return only when the requested operation is **verifiably complete**, not just when the API call succeeds. This creates a better user experience and eliminates race conditions.

### Implementation Guidelines

**1. Configuration Operations (MUST wait by default)**

Tools that modify HA configuration should verify the change is queryable before returning:

```python
async def ha_config_set_automation(config: dict, wait: bool = True) -> dict:
    """Create/update automation.

    Args:
        config: Automation configuration
        wait: If True, poll until automation is queryable (default: True)
    """
    result = await client.send_websocket_message(...)
    entity_id = result.get("entity_id")

    if wait and entity_id:
        # Poll until automation exists in config
        await _wait_for_config_entry(entity_id, timeout=10)

    return result
```

**Applies to:**
- `ha_config_set_automation` - Wait until automation queryable
- `ha_config_set_helper` - Wait until entity registered
- `ha_config_set_label` - Already synchronous (no wait needed)
- `ha_config_set_script` - Wait until script queryable

**2. State-Changing Service Calls (SHOULD wait by default)**

Service calls that change entity state should verify the state change:

```python
async def ha_call_service(domain: str, service: str, entity_id: str = None,
                          wait: bool = True) -> dict:
    """Call a Home Assistant service.

    Args:
        wait: If True, verify expected state change (default: True)
    """
    result = await client.send_websocket_message(...)

    if wait and entity_id and service in STATE_CHANGING_SERVICES:
        expected_state = _infer_expected_state(domain, service)
        await _wait_for_entity_state(entity_id, expected_state, timeout=5)

    return result
```

**Applies to:**
- `light.turn_on/turn_off` - Wait for state "on"/"off"
- `switch.turn_on/turn_off` - Wait for state "on"/"off"
- `automation.trigger` - Wait for state "active" (brief)
- `script.turn_on` - Wait for execution start

**3. Async Operations (CANNOT wait - return immediately)**

Some operations are inherently async and complete over time:

- **Automation execution** - Triggers are async, may take seconds/minutes
- **External integrations** - Cloud API calls, device responses
- **State changes from automations** - Side effects of triggered automations

**Users must poll** using dedicated wait helpers (see E2E test patterns).

**4. Query Operations (no wait needed)**

Read operations return immediately:
- `ha_get_state`, `ha_config_get_automation`, `ha_search_entities`, etc.

### Wait Parameter Design

**Default behavior:**
```python
# Config operations - wait by default (safer, better UX)
await ha_config_set_helper(...)  # Waits until entity registered

# Query operations - return immediately
await ha_get_state(...)  # No wait
```

**Opt-out when needed:**
```python
# Bulk operations - disable wait for performance
for config in bulk_configs:
    await ha_config_set_automation(config, wait=False)

# Then verify all at once
await _verify_all_created(entity_ids)
```

### Migration Path

**Current state (as of #365):**
- Tools return immediately after API call
- E2E tests use polling helpers to wait for completion
- Some operations appear synchronous but have propagation delays

**Future state (see issue #381):**
1. Add `wait` parameter to applicable tools (default `True`)
2. Implement internal polling with sensible timeouts
3. Update tool descriptions to document waiting behavior
4. E2E tests simplified - remove redundant polling

**Backward compatibility:**
- Existing code continues to work (wait defaults to True)
- Tests that poll externally still work (redundant but harmless)
- Performance-sensitive code can opt out with `wait=False`

### Testing Implications

**Before (current):**
```python
# Test must poll to verify completion
create_result = await mcp_client.call_tool("ha_config_set_helper", {...})
entity_id = create_result["entity_id"]
await wait_for_entity_state(mcp_client, entity_id, "off", timeout=10)
```

**After (with tool waiting):**
```python
# Tool waits internally - test just verifies
create_result = await mcp_client.call_tool("ha_config_set_helper", {...})
entity_id = create_result["entity_id"]

# Immediately verify state (no polling needed)
state_result = await mcp_client.call_tool("ha_get_state", {"entity_id": entity_id})
assert state_result["state"] == "off"
```

**Tests still poll for:**
- Logbook entries (async)
- Automation side effects
- Complex multi-step workflows
- External state changes

### References

- Issue #365: Replace fixed sleeps with polling (test-side workaround)
- Issue #381: Add wait parameter to tools (proper fix)

## Context Engineering & Progressive Disclosure

This project applies [context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) and [progressive disclosure](https://www.nngroup.com/articles/progressive-disclosure/) principles to tool design. These complementary approaches help manage cognitive load for both the LLM and the end user.

### Context Engineering

Context engineering treats LLM context as a finite resource with diminishing returns. Rather than front-loading all information, provide the minimum context needed and let the model fetch more when required.

**Guiding principles:**
- **Favor statelessness** — Avoid server-side session tracking or MCP-side state when possible. Use content-derived identifiers (hashes, IDs) that the client can pass back. Example: dashboard updates use content hashing for optimistic locking—hash is computed on read, verified on write to detect conflicts, no session state needed.
- Delegate validation to backend systems when they already handle it well (HA Core uses voluptuous schemas with clear error messages)
- Keep tool parameters simple—let the backend API handle type coercion, defaults, and validation
- Rely on documentation tools rather than embedding extensive docs in every tool description
- Trust that model knowledge + on-demand docs = sufficient context

**Example - pass-through approach:**
```python
# Let HA validate and return its own error messages
message = {"type": f"{helper_type}/create", "name": name}
for param, value in [("latitude", latitude), ("longitude", longitude)]:
    if value is not None:
        message[param] = value
result = await client.send_websocket_message(message)
```

**When tool-side logic adds value:**
- Format normalization for UX convenience (e.g., `"09:00"` → `"09:00:00"`)
- Parsing JSON strings from MCP clients that stringify arrays
- Combining multiple HA API calls into one logical operation

### Progressive Disclosure

[Jakob Nielsen's progressive disclosure](https://www.nngroup.com/articles/progressive-disclosure/) principle: show essential features first, reveal complexity gradually. This applies directly to [LLM context management](https://www.inferable.ai/blog/posts/llm-progressive-context-encrichment)—giving LLMs more context often makes them perform worse by diluting attention.

**How we apply this in ha-mcp:**

| Pattern | Example |
|---------|---------|
| **Docs on demand** | Tool descriptions reference `ha_get_domain_docs()` instead of embedding full documentation |
| **Hints in UX flow** | First tool in a workflow hints at related tools (e.g., `ha_search_entities` suggests `ha_get_state`) |
| **Error-driven discovery** | When a tool fails, the error response hints at `ha_get_domain_docs()` for syntax help |
| **Layered parameters** | Required params first, optional params with sensible defaults |
| **Focused returns** | Return essential data; let user request details via follow-up tools |

**Practical examples in this codebase:**
- `ha_config_set_helper` has minimal docstring, points to `ha_get_domain_docs()` for each helper type
- Search tools return entity IDs and names; full state requires `ha_get_state`
- Error responses include `suggestions` array guiding next steps

### Testing Model Knowledge

Before adding extensive documentation to tool descriptions, test what models already know. Use a **no-context sub-agent** to probe baseline knowledge:

```
Task tool with model=haiku or model=sonnet:
"Without searching or fetching anything, answer from your training data only:
 How do you create a [X] in Home Assistant via WebSocket API?
 What parameters are required vs optional?
 Be honest if you're uncertain."
```

This reveals:
- What the model knows from training (no need to document)
- What gaps exist (target these with `ha_get_domain_docs()` hints)
- Confidence levels across model tiers (haiku vs sonnet vs opus)

**Important: Fact-check model claims.** Models can hallucinate plausible-sounding syntax. Always verify against actual source code or documentation:
```bash
# Check HA Core for actual API schema
gh api /repos/home-assistant/core/contents/homeassistant/components/{domain}/__init__.py \
  --jq '.content' | base64 -d | grep -A 20 "CREATE_FIELDS\|vol.Schema"
```

**Example findings from helper analysis:**
| Model | counter | schedule | zone | tag |
|-------|---------|----------|------|-----|
| Haiku | ~60% confident | ~30% uncertain | ~50% | ~20% |
| Sonnet | ~80% accurate | ~75% knows format | ~85% | ~50% |

This informs whether to embed docs (low model knowledge) or just hint at `ha_get_domain_docs()` (sufficient model knowledge).

### References
- [Anthropic: Effective Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Context Engineering Guide](https://www.promptingguide.ai/guides/context-engineering-guide)
- [Nielsen Norman Group: Progressive Disclosure](https://www.nngroup.com/articles/progressive-disclosure/)
- [Progressive Context Enrichment for LLMs](https://www.inferable.ai/blog/posts/llm-progressive-context-encrichment)

## Home Assistant Add-on

**Required files:**
- `repository.yaml` (root) - For HA add-on store recognition
- `homeassistant-addon/config.yaml` - Must match `pyproject.toml` version

**Docs**: https://developers.home-assistant.io/docs/add-ons

## API Research

Search HA Core without cloning (500MB+ repo):
```bash
# Search for patterns
gh search code "use_blueprint" --repo home-assistant/core path:tests --json path --limit 10

# Fetch file contents (base64 encoded)
gh api /repos/home-assistant/core/contents/homeassistant/components/automation/config.py \
  --jq '.content' | base64 -d > /tmp/ha_config.py
```

**Insight**: Collection-based components (helpers, scripts, automations) follow consistent patterns.

## Test Patterns

**FastMCP validates required params at schema level.** Don't test for missing required params:
```python
# BAD: Fails at schema validation
await mcp.call_tool("ha_config_get_script", {})

# GOOD: Test with valid params but invalid data
await mcp.call_tool("ha_config_get_script", {"script_id": "nonexistent"})
```

**HA API uses singular field names:** `trigger` not `triggers`, `action` not `actions`.

## Release Process

Uses [semantic-release](https://python-semantic-release.readthedocs.io/) with conventional commits.

| Prefix | Bump |
|--------|------|
| `fix:`, `perf:`, `refactor:` | Patch |
| `feat:` | Minor |
| `feat!:` or `BREAKING CHANGE:` | Major |
| `chore:`, `docs:`, `test:` | No release |

| Channel | When Updated |
|---------|--------------|
| Dev (`.devN`) | Every master commit |
| Stable | Weekly (Tuesday 10:00 UTC) |

Manual release: Actions > SemVer Release > Run workflow.

## Custom Agents

Located in `.claude/agents/`:

| Agent | Purpose |
|-------|---------|
| `triage` | Triage issues, assess complexity, update labels |
| `issue-to-pr-resolver` | End-to-end: issue → branch → implement → PR → CI green |
| `pr-checker` | Review PR comments, resolve threads, monitor CI |

## Documentation Updates

Update this file when:
- Discovering workflow improvements
- Solving non-obvious problems
- API/test patterns learned

**Rule:** If you struggled with something, document it for next time.
