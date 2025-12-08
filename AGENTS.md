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
Branch from `stable` tag, not master:
```bash
git checkout -b hotfix/description stable
gh pr create --base master
```

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
docker run -d -p 8086:8086 -e HOMEASSISTANT_URL=... -e HOMEASSISTANT_TOKEN=... ghcr.io/homeassistant-ai/ha-mcp:latest fastmcp run fastmcp-http.json
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

## Home Assistant Add-on

**Required files:**
- `repository.yaml` (root) - For HA add-on store recognition
- `homeassistant-addon/config.yaml` - Must match `pyproject.toml` version

**Docs**: https://developers.home-assistant.io/docs/add-ons

## API Research

Finding undocumented HA APIs (don't clone the huge repo):
```bash
gh api /search/code -X GET -f q="helper list websocket repo:home-assistant/core" -f per_page=5 --jq '.items[] | {name, path, url: .html_url}'
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
