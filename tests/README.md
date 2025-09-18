# 🧪 Home Assistant MCP Tests

## 🚀 Quick Start

### Interactive Test Environment (Recommended)

```bash
# Start test environment with menu
uv run hamcp-test-env
```

**Features:**
- 🐳 Auto-managed Home Assistant container
- 📋 Interactive menu (run tests, view status, shutdown)
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
6. **Update token**: Replace token in `tests/test_env_manager.py` `ha_token` variable

## ⚡ Performance

- **Basic tests**: ~30-60s
- **All E2E tests**: ~20-45m
- **Container startup**: ~30-60s

Use `-m "not slow"` for faster development iterations.