# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 🚨 Git Branch Policy

**CRITICAL: Never commit directly to master branch!**

- **Always create a feature branch** before making any changes
- Use naming convention: `feature/description` or `fix/description`
- Example: `git checkout -b feature/add-new-tool`
- A pre-commit hook is installed to prevent direct commits to master
- All changes must go through Pull Requests

```bash
# Correct workflow
git checkout -b feature/your-feature
# make changes
git add . && git commit -m "your changes"
# ASK USER before pushing or creating PRs
# git push -u origin feature/your-feature
# Create PR on GitHub
```

## 🚨 Push and PR Policy

**CRITICAL: Always ask user permission before pushing or creating Pull Requests!**

- **Never push to remote** without explicit user consent
- **Never create PRs** without user approval
- User must explicitly request push/PR operations
- Commit locally first, then ask user for next steps

## 🔄 PR Default Workflow

**Standard workflow when user requests PR submission:**

1. **Update tests** - Check if tests need updates for your changes
2. **Commit and push** - Commit all changes and push to feature branch
3. **Wait 3 minutes** - GitHub Actions tests take ~2-3 minutes to run
4. **Check PR status** - Run `gh pr checks <PR-number>` to verify all checks pass
5. **Fix failures** - If tests fail, fix issues and repeat from step 2
6. **Report to user** - Inform user of test results

```bash
# Example workflow
git add -A && git commit -m "feat: description" && git push
sleep 180  # Wait 3 minutes
gh pr checks 8  # Check status
```

**Test failure handling:**
- Check logs: `gh run view <run-id> --log-failed`
- Fix code and push again
- Tests auto-run on every push

## 📝 Updating This File (AGENTS.md)

**When to update AGENTS.md:**

1. **After discovering workflow improvements** - Document patterns that work well
2. **When solving non-obvious problems** - Add to relevant sections for future reference
3. **Before completing a PR** - Ask user: "Should we add anything to AGENTS.md?"
4. **Automatic updates** - If improvement is obviously beneficial, update proactively

**What to document:**
- API discovery techniques that worked
- Test patterns that solved problems
- Configuration gotchas and solutions
- Tool design patterns learned
- Build/deployment lessons

**Rule of thumb:** If you struggled with something, document it so next time is easier!

# Home Assistant MCP Server

A production-ready Model Context Protocol (MCP) server that enables AI assistants to control Home Assistant smart home systems through REST API and WebSocket connections. The project provides fuzzy search, real-time monitoring, and AI-optimized device control with comprehensive test coverage.

## Development Commands

### Tool Usage Logging

- Verbose tool request/response logging is controlled via the
  `HOMEASSISTANT_TOOL_LOG_DIR` environment variable. When enabled, see
  [`docs/tool-usage-logging.md`](docs/tool-usage-logging.md) for parsing
  scripts and optimization workflow tips.
- CI pushes trigger the `E2E Tests` workflow and pull requests trigger the `PR
  Validation Pipeline`; both store log output in `artifacts/tool_calls.ndjson.zst` and
  run `tests/src/tool_logging/` afterwards to ensure verbose logging and
  analysis helpers continue to function. The PR workflow delegates to the
  reusable `E2E Tests` pipeline so push and PR runs share identical logging
  steps.

### Environment Setup
```bash
# Install dependencies (UV required)
uv sync

# Install with development dependencies  
uv sync --group dev

# Run the main MCP server (20+ tools)
uv run ha-mcp

# Or run directly via module
uv run python -m ha_mcp
```

### Configuration
Copy `.env.example` to `.env` and configure your Home Assistant connection:
```bash
cp .env.example .env
# Edit .env with your Home Assistant URL and token
```

### Quick Start Scripts
```bash
# Windows
./run_mcp_server.bat

# Linux/macOS
./run_mcp_server.sh
```

### Testing Commands

#### End-to-End (E2E) Tests

**IMPORTANT: Test paths corrected in v1.0.3+**
- E2E tests are in `tests/src/e2e/` NOT `tests/e2e/`
- Test runner is at `tests/src/e2e/run_tests.py`

```bash
# Prerequisites: Tests use testcontainers - Docker daemon must be running
# No manual container setup needed - tests auto-create fresh HA instances

# Run all E2E tests (uses testcontainers)
HAMCP_ENV_FILE=tests/.env.test uv run python tests/src/e2e/run_tests.py

# Run fast tests only (excludes @pytest.mark.slow tests)
HAMCP_ENV_FILE=tests/.env.test uv run python tests/src/e2e/run_tests.py fast

# Run using pytest directly (recommended for development)
HAMCP_ENV_FILE=tests/.env.test uv run pytest tests/src/e2e/ -v --tb=short

# Run single test
HAMCP_ENV_FILE=tests/.env.test uv run pytest tests/src/e2e/workflows/automation/test_lifecycle.py::TestAutomationLifecycle::test_basic_automation_lifecycle -v

# Run specific test category
HAMCP_ENV_FILE=tests/.env.test uv run pytest tests/src/e2e/workflows/automation/ -v
HAMCP_ENV_FILE=tests/.env.test uv run pytest tests/src/e2e/workflows/scripts/ -v
HAMCP_ENV_FILE=tests/.env.test uv run pytest tests/src/e2e/error_handling/ -v
```

#### Interactive Test Environment (hamcp-test-env)

**Quick, isolated Home Assistant environment for development, testing, and API exploration.**

**Features:**
- 🐳 Auto-managed Docker container with testcontainers
- 🚀 Ready in ~30 seconds
- 🔑 Pre-configured auth token for immediate API access
- 📋 Copy-paste environment variables for testing
- 🌐 Web UI access for manual inspection
- 🔄 Can run tests multiple times without restart
- 🧹 Automatic cleanup on exit

**Usage Patterns:**

```bash
# Pattern 1: Non-interactive mode for API testing (recommended for automation)
# The Bash tool automatically backgrounds commands that exceed timeout
uv run hamcp-test-env --no-interactive 2>&1
# Command will auto-background after 30s, wait for it to be ready
sleep 30
# Container is now running, copy-paste the export lines from output
export HOMEASSISTANT_URL=http://localhost:PORT
export HOMEASSISTANT_TOKEN=eyJhbG...
# Do your testing
curl -H "Authorization: Bearer $HOMEASSISTANT_TOKEN" $HOMEASSISTANT_URL/api/config | jq
# Stop by killing the background shell when done

# Pattern 2: Interactive mode for running E2E tests
uv run hamcp-test-env
# Wait for status banner showing URL and token
# Choose option 1 to run tests
# Choose option 3 to show status again
# Choose option 2 to stop and exit

# Pattern 3: Quick one-liner API validation
# Start environment, wait, test, and you're done
uv run hamcp-test-env --no-interactive 2>&1  # Will auto-background
sleep 30
curl -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..." http://localhost:PORT/api/
```

**Startup Banner provides:**
- Web UI URL with username/password (mcp/mcp)
- Copy-pasteable environment variable exports
- Full API token for curl/scripts
- API health status

**Use Cases:**
- Test API endpoints manually before writing tests
- Validate tool implementations against real HA instance
- Debug WebSocket connections
- Explore Home Assistant API behavior
- Quick smoke tests during development

**Important:**
- Docker daemon must be running
- Port is randomly assigned (shown in startup banner)
- Container auto-cleans up on exit (Ctrl+C or option 2)
- Use `--no-interactive` for non-interactive/automated usage
- Interactive mode requires stdin for menu navigation
- **Test token is centralized in `tests/test_constants.py`** - all test code imports from this single location to avoid duplication and typos

### Code Quality Commands
```bash
# Format code
uv run black src/ tests/
uv run isort src/ tests/

# Lint code
uv run ruff check src/ tests/
uv run ruff check --fix src/ tests/

# Type checking
uv run mypy src/
```

### Docker Commands

#### Production Docker Image (ghcr.io/homeassistant-ai/ha-mcp)

**Built automatically** via GitHub Actions on every release.

**Default mode: stdio** (for MCP clients like Claude Desktop)
```bash
# Pull the latest image
docker pull ghcr.io/homeassistant-ai/ha-mcp:latest

# Run in stdio mode (default)
docker run --rm -i \
  -e HOMEASSISTANT_URL=http://homeassistant.local:8123 \
  -e HOMEASSISTANT_TOKEN=your_token \
  ghcr.io/homeassistant-ai/ha-mcp:latest

# Use in mcp.json for Claude Desktop:
{
  "mcpServers": {
    "home-assistant": {
      "command": "docker",
      "args": ["run", "--rm", "-i",
               "-e", "HOMEASSISTANT_URL=http://host.docker.internal:8123",
               "-e", "HOMEASSISTANT_TOKEN=your_token",
               "ghcr.io/homeassistant-ai/ha-mcp:latest"]
    }
  }
}
```

**HTTP mode** (for Claude Code, remote clients)
```bash
# Run in streamable-http mode
docker run -d --name ha-mcp \
  -p 8086:8086 \
  -e HOMEASSISTANT_URL=http://homeassistant.local:8123 \
  -e HOMEASSISTANT_TOKEN=your_token \
  ghcr.io/homeassistant-ai/ha-mcp:latest \
  fastmcp run fastmcp-http.json

# Check logs
docker logs ha-mcp -f

# Stop and remove
docker stop ha-mcp && docker rm ha-mcp
```

**Key features:**
- **ENTRYPOINT**: `uv run --no-project` (runs commands with system packages)
- **Default CMD**: `fastmcp run fastmcp.json` (stdio mode)
- **HTTP mode**: Override with `fastmcp run fastmcp-http.json`

#### Local Docker Build

```bash
# Build locally from source
docker build -t ha-mcp:local .

# Run in stdio mode
docker run --rm -i \
  -e HOMEASSISTANT_URL=http://homeassistant.local:8123 \
  -e HOMEASSISTANT_TOKEN=your_token \
  ha-mcp:local

# Run in HTTP mode
docker run -d --name ha-mcp-local \
  -p 8086:8086 \
  -e HOMEASSISTANT_URL=http://homeassistant.local:8123 \
  -e HOMEASSISTANT_TOKEN=your_token \
  ha-mcp:local \
  fastmcp run fastmcp-http.json
```

#### Docker Test Environment (for E2E tests)

```bash
# Initialize and start test Home Assistant instance
cd tests/
./init_test_env.sh               # Copy initial state to haconfig/
docker compose up -d             # Start container on port 8124
docker logs homeassistant-test -f  # Watch startup logs

# Test API connectivity
curl -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiIxOTE5ZTZlMTVkYjI0Mzk2YTQ4YjFiZTI1MDM1YmU2YSIsImlhdCI6iTc1NzI4OTc5NiwiZXhwIjoyMDcyNjQ5Nzk2fQ.Yp9SSAjm2gvl9Xcu96FFxS8SapHxWAVzaI0E3cD9xac" http://localhost:8124/api/config

# Reset environment to initial state
docker compose down
./init_test_env.sh
docker compose up -d

# Clean up test environment
docker compose down
```

### Test Data and Environment States
```bash
# Test environment state snapshots are saved to tests/data/
# These files document the available entities in the Docker test environment

# View latest test environment snapshot
ls -la tests/data/test_environment_state_*.json

# The snapshots contain:
# - Available entities by domain (light, climate, cover, etc.)
# - Entity counts and examples
# - Home Assistant version and configuration
# - Recommended test entities for each domain

# Test entities available in Docker environment:
# - Lights: light.bed_light, light.ceiling_lights, light.kitchen_lights, etc.
# - Climate: climate.ecobee, climate.heatpump, climate.hvac
# - Covers: cover.kitchen_window, cover.garage_door, cover.pergola_roof
# - Switches: switch.ac, switch.decorative_lights, etc.
# - Sensors: Many available for monitoring tests
```

### Home Assistant Add-on Repository Requirements

**Critical File**: `repository.yaml` at project root

This file is **required** for Home Assistant to recognize the repository as an add-on repository. Without it, the add-on will not appear in the add-on store.

**Structure**:
```yaml
name: Home Assistant MCP Server
url: 'https://github.com/homeassistant-ai/ha-mcp'
maintainer: Julien <github@qc-h.net>
```

**Required files for add-on**:
- `repository.yaml` - Repository metadata (root level)
- `homeassistant-addon/config.yaml` - Add-on configuration
- `homeassistant-addon/Dockerfile` - Container build instructions
- `homeassistant-addon/start.py` - Startup script
- `homeassistant-addon/README.md` - Add-on documentation
- `homeassistant-addon/DOCS.md` - Detailed documentation

**Version sync**: The version in `homeassistant-addon/config.yaml` must match `pyproject.toml` version.

**Documentation**: Official Home Assistant add-on docs at https://developers.home-assistant.io/docs/add-ons/

## Architecture Overview

### Core Components Architecture
The codebase follows a modular architecture with clear separation of concerns:

```
Home Assistant MCP Server - Current Structure
├── Core Server (/src/ha_mcp/)
│   ├── server.py              # Main server implementation with FastMCP
│   ├── __main__.py            # FastMCP entrypoint (dual CLI/FastMCP support)
│   └── config.py              # Configuration management with Pydantic
├── Client Layer (/src/ha_mcp/client/)
│   ├── rest_client.py         # HTTP REST API client
│   ├── websocket_client.py    # WebSocket client for real-time monitoring
│   └── websocket_listener.py  # Background WebSocket listener service
├── Tools Layer (/src/ha_mcp/tools/)
│   ├── registry.py            # Centralized tool registration
│   ├── smart_search.py        # Fuzzy entity search and AI tools
│   ├── device_control.py      # Smart device control with WebSocket verification
│   ├── convenience.py         # Scene/automation/weather convenience tools
│   └── enhanced.py            # Enhanced tool implementations
├── Resources Layer (/src/ha_mcp/resources/)
│   └── manager.py             # MCP resource management
├── Prompts Layer (/src/ha_mcp/prompts/)
│   ├── manager.py             # MCP prompt templates
│   └── enhanced.py            # Enhanced prompts
└── Utils Layer (/src/ha_mcp/utils/)
    ├── fuzzy_search.py        # Fuzzy matching engine with fuzzywuzzy
    ├── domain_handlers.py     # Home Assistant domain-specific logic
    ├── operation_manager.py   # Async operation management
    └── usage_logger.py        # Tool usage logging
```

### Key Design Patterns

#### Tools Registry Pattern
- **Central Registration**: `tools/registry.py` manages all 20+ MCP tools in one place
- **Decorator-Based**: Uses `@log_tool_usage` for automatic logging and metrics
- **Type Safety**: All tools use Pydantic models for parameter validation

#### Async Operation Management
- **WebSocket Verification**: Device operations verified via WebSocket state changes
- **Operation Tracking**: In-memory tracking with timeouts for async operations
- **Bulk Operations**: Parallel execution of multiple device commands

### Home Assistant Integration Points

#### REST API Integration
- **Client Architecture**: `HomeAssistantClient` handles HTTP requests with retries
- **OpenAPI Auto-Generation**: FastMCP automatically generates tools from HA OpenAPI spec
- **Domain Handlers**: Special logic for lights, climate, covers, media players, etc.

#### WebSocket Integration
- **Real-time Monitoring**: `WebSocketClient` for state change verification
- **Event Streaming**: Listen for state changes to confirm operations completed
- **Connection Management**: Auto-reconnect with exponential backoff

#### Smart Search Engine
- **Fuzzy Matching**: Uses `fuzzywuzzy` with `python-levenshtein` for performance
- **Multi-language**: Supports French/English entity naming conventions
- **Area-Based Search**: Groups entities by Home Assistant areas/rooms
- **AI Optimization**: Provides system overviews optimized for AI understanding

### Testing Strategy
- **Environment Isolation**: Docker-based test Home Assistant instance
- **Comprehensive Coverage**: Tests all 20+ tools with real Home Assistant API calls
- **Snapshot Management**: Clean baseline and token-configured snapshots for testing
- **Integration Testing**: Full end-to-end testing with WebSocket operations

### Error Handling Patterns
- **Graceful Degradation**: Operations continue even if WebSocket verification fails
- **Timeout Management**: Configurable timeouts for operations and connections
- **Sanitized Responses**: Error messages sanitized to prevent token leakage
- **Retry Logic**: Exponential backoff for network operations

### Performance Optimizations
- **Connection Pooling**: HTTP client reuses connections
- **Parallel Operations**: Bulk device control supports parallel execution
- **Fuzzy Search Caching**: Search results cached for improved performance
- **WebSocket Persistence**: Single WebSocket connection reused across operations

## 🔍 Home Assistant API Research

**Finding undocumented Home Assistant APIs:**

When implementing new features that require Home Assistant API endpoints not in the official docs:

1. **Use GitHub code search** with `gh` CLI (don't clone the massive home-assistant/core repo):
   ```bash
   # Example: Finding helper list endpoint
   gh api /search/code \
     -X GET \
     -f q="helper list websocket repo:home-assistant/core" \
     -f per_page=5 \
     --jq '.items[] | {name: .name, path: .path, url: .html_url}'
   ```

2. **Search patterns that work well:**
   - WebSocket endpoints: `"{entity_type}/list" "websocket" repo:home-assistant/core`
   - REST endpoints: `"api_routes" "{domain}" repo:home-assistant/core`
   - Component internals: `"class {ComponentName}" repo:home-assistant/core`

3. **Example discoveries:**
   - Found `{helper_type}/list` websocket endpoint in `collection.py`
   - Pattern: `DictStorageCollectionWebsocket` provides `list` endpoint for collection types
   - Applies to: input_boolean, input_number, input_select, input_text, input_datetime, input_button

**Key insight:** Home Assistant's collection-based components (helpers, scripts, automations) follow consistent patterns. If one has a feature, others likely do too.

## 🧪 Test Development Patterns

**Common test pitfalls and solutions:**

### FastMCP Parameter Validation vs Tool Validation

**Problem:** Tests that validate "missing required parameter" will fail with FastMCP.

**Why:** FastMCP validates required parameters at schema level BEFORE tool code runs.

**Solution:** Don't test for missing required parameters - FastMCP handles this automatically.

```python
# ❌ BAD - This will fail with FastMCP validation error
async def test_error_handling():
    result = await mcp.call_tool_failure(
        "ha_config_get_script",
        {},  # Missing required script_id
        expected_error="script_id is required"
    )

# ✅ GOOD - Test tool's internal validation
async def test_error_handling():
    result = await mcp.call_tool_failure(
        "ha_config_get_script",
        {"script_id": "nonexistent"},  # Valid params, invalid data
        expected_error="not found"
    )
```

### Test Data Factory Pattern

**Use `test_data_factory` fixture** for creating test configs:

```python
# Automation config factory
config = test_data_factory.automation_config(
    "Morning Routine",
    trigger=[{"platform": "time", "at": "07:00:00"}],
    action=[{"service": "light.turn_on", "target": {"entity_id": "light.bedroom"}}]
)
```

**Important:** Home Assistant API uses **singular** field names:
- `trigger` NOT `triggers`
- `action` NOT `actions`
- TestDataFactory now returns singular fields (fixed in v1.0.3)

### Parameter Structure Updates After API Refactoring

When refactoring action-based APIs to split functions:

**Before (action-based):**
```python
await mcp.call_tool("ha_manage_helper", {
    "action": "create",
    "helper_type": "input_boolean",
    "name": "test_bool"
})
```

**After (split functions):**
```python
await mcp.call_tool("ha_config_set_helper", {
    "helper_type": "input_boolean",
    "name": "test_bool"
    # No 'action' parameter - implicit in function name
})
```

**Test updates needed:**
1. Remove `action` parameter from calls
2. Update tool names
3. For delete operations: only keep domain-specific params (no `name=""` cruft)
4. Use automated transformation where possible (sed scripts, bulk find/replace)

## 📦 Semantic Versioning with semantic-release

**Commit message format controls version bumps:**

```bash
# Patch bump (1.0.0 → 1.0.1)
fix: bug description
perf: performance improvement
refactor: code refactoring

# Minor bump (1.0.0 → 1.1.0)
feat: new feature description

# Major bump (1.0.0 → 2.0.0)
feat!: breaking change description
# OR
feat: description

BREAKING CHANGE: explanation of breaking change

# No version bump
chore: maintenance task
docs: documentation update
test: test changes
```

**Configuration location:** `pyproject.toml` under `[tool.semantic_release]`