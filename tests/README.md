# 🧪 Home Assistant MCP Tests

## 🚀 Quick Start

### Interactive Test Environment (Recommended)

```bash
# Start test environment with interactive menu
uv run hamcp-test-env

# Or start in non-interactive mode (for automation/background usage)
uv run hamcp-test-env --no-interactive
```

**Features:**
- 🐳 Auto-managed Home Assistant container
- 📋 Interactive menu (run tests, view status, shutdown)
- 🤖 Non-interactive mode for automation (use `--no-interactive`)
- 🌐 Web UI access: `mcp` / `mcp`
- 🔄 Multiple test runs without restart

### Direct pytest

```bash
# All E2E tests
uv run pytest tests/src/e2e/ -v

# Fast tests only
uv run pytest tests/src/e2e/ -v -m "not slow"

# Specific categories
uv run pytest tests/src/e2e/basic/ -v               # Basic connectivity
uv run pytest tests/src/e2e/workflows/automation/ -v # Automation tests
```

### Manual Branch Testing

Need to validate unpublished changes from a feature branch? Use `uvx` with the
Git URL so you can run the project directly from that branch:

```bash
# FastMCP STDIO entry point
uvx --from git+https://github.com/homeassistant-ai/ha-mcp.git@branchname ha-mcp

# FastMCP HTTP server
uvx --from git+https://github.com/homeassistant-ai/ha-mcp.git@branchname ha-mcp-web
```

Replace `branchname` with the branch you want to exercise (for example,
`feature/manual-test-instructions`). If you are working from a fork, swap
`homeassistant-ai` with your GitHub username to target your repository in both
commands. When using these commands, adapt the corresponding `uvx` setup in
[README.md → Method 3: Running Python with UV](../README.md#method-3-running-python-with-uv)
so your environment variables and client configuration match the guidance in the
main installation instructions. This ensures your manual testing matches the
code under review.

## 📁 Structure

```
tests/
├── src/e2e/                    # All test files
│   ├── basic/                  # Connection & basic tests
│   ├── workflows/              # Complex scenarios
│   └── error_handling/         # Error scenarios
├── initial_test_state/         # Clean HA config baseline
├── test_env_manager.py         # Interactive test runner
└── pytest.ini                 # Test configuration
```

## 🔧 Test Categories

- **Basic**: Connection, tool listing, entity search
- **Workflows**: Automation, device control, scripts, scenes
- **Error Handling**: Invalid inputs, network failures

## 🐛 Debugging

1. Start: `uv run hamcp-test-env`
2. Access Web UI with displayed URL + `mcp`/`mcp` credentials
3. Run tests via menu or separate terminal
4. Inspect states in Web UI between runs

## 🔄 Updating Test Environment

To update the baseline Home Assistant configuration:

1. **Clear baseline**: `rm -rf tests/initial_test_state/*`
2. **Start container**: `uv run hamcp-test-env`
3. **Setup HA**:
   - Access Web UI with displayed URL
   - Create user: `mcp` / password: `mcp`
   - Generate Personal Access Token
4. **Shutdown**: Choose option 2 in menu
5. **Save state**: Copy files from displayed temp directory to `tests/initial_test_state/`
6. **Update token**: Replace `TEST_TOKEN` in `tests/test_constants.py` (centralized location for all test code)
