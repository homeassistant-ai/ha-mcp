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
git push -u origin feature/your-feature
# Create PR on GitHub
```

# Home Assistant MCP Server

A production-ready Model Context Protocol (MCP) server that enables AI assistants to control Home Assistant smart home systems through REST API and WebSocket connections. The project provides fuzzy search, real-time monitoring, and AI-optimized device control with comprehensive test coverage.

## Development Commands

### Environment Setup
```bash
# Install dependencies (UV required)
uv sync

# Install with development dependencies  
uv sync --group dev

# Run the main MCP server (smart server with 20+ tools)
uv run homeassistant-mcp

# Or run directly via module
uv run python -m homeassistant_mcp.smart_server
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
setup/run_mcp_server.bat

# Linux/macOS
setup/run_mcp_server.sh
```

### Testing Commands

#### End-to-End (E2E) Tests
```bash
# Prerequisites: Docker test environment must be running on port 8124
# Start test environment: cd tests/ && docker compose up -d

# Run all E2E tests
HAMCP_ENV_FILE=tests/.env.test uv run python tests/e2e/run_tests.py

# Run fast tests only (excludes slow tests)
HAMCP_ENV_FILE=tests/.env.test uv run python tests/e2e/run_tests.py fast

# Run specific test scenarios
HAMCP_ENV_FILE=tests/.env.test uv run python tests/e2e/run_tests.py automation    # Automation lifecycle
HAMCP_ENV_FILE=tests/.env.test uv run python tests/e2e/run_tests.py device       # Device control
HAMCP_ENV_FILE=tests/.env.test uv run python tests/e2e/run_tests.py script       # Script orchestration
HAMCP_ENV_FILE=tests/.env.test uv run python tests/e2e/run_tests.py helper       # Helper integration
HAMCP_ENV_FILE=tests/.env.test uv run python tests/e2e/run_tests.py error        # Error handling
HAMCP_ENV_FILE=tests/.env.test uv run python tests/e2e/run_tests.py scenarios    # All scenarios

# Run using pytest directly
HAMCP_ENV_FILE=tests/.env.test uv run pytest tests/e2e/ -v --tb=short

# Run single E2E test
HAMCP_ENV_FILE=tests/.env.test uv run pytest tests/e2e/scenarios/test_automation_lifecycle.py::TestAutomationLifecycle::test_basic_automation_lifecycle -v
```

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

### Docker Test Environment
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

## Architecture Overview

### Core Components Architecture
The codebase follows a modular architecture with clear separation of concerns:

```
FastMCP Server (Enhanced) - Reorganized Structure
├── Core Server (/src/homeassistant_mcp/)
│   ├── server.py              # Main server implementation (was server/core.py)
│   ├── __main__.py            # FastMCP entrypoint (dual CLI/FastMCP support)
│   ├── cli.py                 # CLI interface (was server/cli.py)
│   └── config.py              # Configuration management with Pydantic
├── Client Layer (/src/homeassistant_mcp/client/)
│   ├── rest_client.py         # HTTP REST API client (was client.py)
│   ├── websocket_client.py    # WebSocket client for real-time monitoring
│   └── websocket_listener.py  # Background WebSocket listener service
├── Tools Layer (/src/homeassistant_mcp/tools/)
│   ├── registry.py            # Centralized tool registration (was server/tools_registry.py)
│   ├── smart_search.py        # Fuzzy entity search and AI tools
│   ├── device_control.py      # Smart device control with WebSocket verification
│   ├── convenience.py         # Scene/automation/weather convenience tools
│   └── enhanced.py            # Enhanced tool implementations (was server/enhanced_tools.py)
├── Resources Layer (/src/homeassistant_mcp/resources/)
│   └── manager.py             # MCP resource management (was server/resources.py)
├── Prompts Layer (/src/homeassistant_mcp/prompts/)
│   ├── manager.py             # MCP prompt templates (was server/prompts.py)
│   └── enhanced.py            # Enhanced prompts (was server/enhanced_prompts.py)
└── Utils Layer (/src/homeassistant_mcp/utils/)
    ├── fuzzy_search.py        # Fuzzy matching engine with fuzzywuzzy
    └── domain_handlers.py     # Home Assistant domain-specific logic
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