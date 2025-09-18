# Contributing to Home Assistant MCP Server

Thank you for your interest in contributing to the Home Assistant MCP Server project!

## 🤝 Getting Started

1. **Fork the repository**
2. **Clone your repo**: `git checkout ...`
3. **Run tests to make sure your environment works (docker required)**: `uv run pytest tests/src/e2e/ -v`
4. **Make your changes**
5. **Commit changes**: `git commit -m 'Add amazing feature'`
6. **Push to branch**: `git push origin`
7. **Open Pull Request in the Github interface from your repo**

## 🧪 Testing

I like e2e. Tests run with a real Home Assistant instance, in a container with demo mode enabled.

For comprehensive testing documentation, including setup instructions, debugging, and test modes, please see: **[tests/README.md](tests/README.md)**

### Quick Test Instructions
```bash
# Prerequisites: Docker installed (uses Testcontainers)

# Run all E2E tests
uv run pytest tests/src/e2e/ -v
```

The E2E test suite validates:
- ✅ Home Assistant connectivity and API integration
- ✅ All 20+ MCP tools functionality
- ✅ Smart search and fuzzy matching
- ✅ Device control with WebSocket verification
- ✅ Complete automation and script lifecycle
- ✅ Helper entity management
- ✅ Error handling and edge cases

## 🛠️ Development Setup

### Prerequisites
- **uv** Package manager. See https://docs.astral.sh/uv/getting-started/installation/
- **Docker** for running tests
- **Long-lived access token** from Home Assistant user profile

### Environment Setup
```bash
# Install dependencies
uv sync --group dev

# Copy environment template
cp .env.example .env
# Edit .env with your Home Assistant details
```

### Code Quality
```bash
# Format code
uv run ruff format src/ tests/

# Lint code
uv run ruff check src/ tests/
uv run ruff check --fix src/ tests/

# Type checking
uv run mypy src/
```

## 📋 Contribution Guidelines

### Code Style
- Follow existing code patterns and conventions
- Use type hints for all functions and methods
- Add docstrings for public APIs
- Keep functions focused and well-named

### Testing
- Add tests for new features
- Ensure all existing tests pass
- Test with real Home Assistant instance when possible
- Document any new test scenarios

### Documentation
- Update README.md if adding user-facing features
- Add docstrings to new functions and classes
- Update this CONTRIBUTING.md if changing development workflow

### Pull Request Process
1. Update the README.md with details of changes if applicable
2. Increase version numbers in any examples files and the README.md if applicable
3. Ensure tests pass and code follows the project style
4. The PR will be merged once you have the sign-off of a project maintainer

## 🏗️ Architecture Overview

The project follows a modular architecture:

```
src/homeassistant_mcp/
├── server/                          # FastMCP server implementation
│   ├── core.py                      # Main server and tool registration
│   ├── tools_registry.py            # Centralized tool management
│   └── cli.py                       # Command-line interface
├── client.py                        # Home Assistant HTTP client
├── websocket/                       # WebSocket connectivity
│   ├── client.py                    # WebSocket client implementation
│   └── listener.py                  # Event streaming and monitoring
├── tools/                           # MCP tools implementation
│   ├── smart_search.py              # Fuzzy search and AI tools
│   ├── device_control.py            # Device control with verification
│   └── convenience.py               # Automation and scene tools
└── utils/                           # Utilities and helpers
    ├── fuzzy_search.py              # Search algorithms
    ├── domain_handlers.py           # HA domain-specific logic
    ├── operation_manager.py         # Async operation tracking
    └── usage_logger.py              # Tool usage logging
```

## 🔧 Development Tools

### Available Scripts
```bash
# Run the server
uv run homeassistant-mcp

# Run with FastMCP
uv run fastmcp run

# Run tests
uv run pytest tests/src/e2e/ -v

# Format and lint
uv run ruff format . && uv run ruff check --fix .
```

## 🐛 Debugging

### Common Issues
1. **Connection errors**: Check your Home Assistant URL and token
2. **Test failures**: Ensure Docker is running and accessible
3. **Import errors**: Run `uv sync` to ensure all dependencies are installed

### Debugging Tests
```bash
# Run single test with verbose output
uv run pytest tests/src/e2e/basic/test_connection.py -v -s

# Run tests with debug logging
DEBUG=1 uv run pytest tests/src/e2e/ -v
```

## 📞 Getting Help

- Check existing [Issues](../../issues) for similar problems
- Open a new issue with detailed description and steps to reproduce
- Join discussions in [Discussions](../../discussions)

Thank you for contributing to make Home Assistant more accessible through AI assistants! 🎉