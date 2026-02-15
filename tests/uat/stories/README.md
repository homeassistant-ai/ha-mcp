# User Acceptance Stories

User acceptance stories for ha-mcp Bot Acceptance Testing (BAT). Each story represents a realistic use case that users perform with AI agents connected to Home Assistant via MCP.

## Purpose

These stories serve as:
1. **Regression detection** - Run before stable releases to catch behavioral regressions
2. **Tool refactoring validation** - Compare agent behavior before/after tool changes
3. **Benchmark** - Measure agent capability and efficiency across common tasks (billable tokens, tool calls, success rate)

## How Stories Work

Each story is a YAML file in `catalog/` with:

```yaml
id: s01
title: "Create sunset lights automation"
category: automation
weight: 5  # importance 1-5 (5 = most critical)
description: >
  What the user wants to accomplish and why this matters.

# Setup: MCP tool calls executed via FastMCP in-memory (fast, deterministic)
setup:
  - tool: ha_config_set_helper
    args: {helper_type: "input_boolean", name: "Test Toggle"}

# Test: Natural language prompt sent to AI agent via BAT runner
prompt: >
  Create an automation that turns on the porch light at sunset.
  Report what you created.

# Teardown removed — containers are ephemeral (one per agent)
teardown: []

# Verification questions asked against live HA after the test
verify:
  questions:
    - "Does an automation with alias 'Sunset Porch Light' exist?"
    - "Does it have sun triggers for sunset and sunrise?"

# Expected outcomes for evaluation
expected:
  tools_should_use:
    - ha_config_set_automation
```

## Running Stories

### Via pytest (recommended)

```bash
# Run all stories
uv run pytest tests/uat/stories/ -v

# Run a specific category
uv run pytest tests/uat/stories/ -v -k "automation"

# Run only high-weight stories (weight >= 4)
uv run pytest tests/uat/stories/ -v -m "critical"
```

### Standalone via story runner

```bash
# Run a single story (each agent gets a fresh HA container)
uv run python tests/uat/stories/run_story.py catalog/s01_automation_sunset_lights.yaml --agents gemini

# Run all stories
uv run python tests/uat/stories/run_story.py --all --agents gemini

# Run against a specific release tag (for comparison)
uv run python tests/uat/stories/run_story.py --all --agents gemini --branch v6.6.1

# Keep container alive after run (for verification)
uv run python tests/uat/stories/run_story.py catalog/s01_automation_sunset_lights.yaml --agents gemini --keep-container
```

## Story Design Principles

1. **Realistic**: Stories reflect what users actually ask AI agents to do
2. **Multi-step**: Each story combines 2-3 tool capabilities (not just "turn on a light")
3. **Management-focused**: HA instance management (automations, dashboards, helpers) > device control
4. **Test-env compatible**: Stories work within the demo HA test instance
5. **Concise prompts**: Natural language, like a user would actually type
6. **Deterministic setup**: Programmatic setup via FastMCP ensures consistent initial state

## Adding New Stories

1. Create a YAML file in `catalog/` following the naming convention: `sNN_category_brief_name.yaml`
2. Set an appropriate weight (1-5) based on how common the use case is
3. Ensure setup creates any needed state (teardown is not needed — containers are ephemeral)
4. Add `verify.questions` for black-box evaluation against the live HA instance
5. Test that the story works: `uv run pytest tests/uat/stories/ -v -k "sNN"`
6. Add a brief entry to `TODO.md` if you discover follow-up stories during testing

## Categories

| Category | Description | Example |
|----------|-------------|---------|
| `automation` | Create, edit, debug, trace automations | Sunset lights, motion detection |
| `dashboard` | Dashboard and card management | Room overview, energy monitoring |
| `script` | Script creation and management | Goodnight routine, morning sequence |
| `helper` | Input helpers and derived entities | Vacation mode toggle, counters |
| `entity` | Entity discovery, search, state checking | "What devices do I have?" |
| `organization` | Areas, labels, groups, floors | Organize entities into rooms |
| `troubleshoot` | Debug, history, traces, logbook | "Why didn't my automation fire?" |
| `calendar` | Calendar event management | Maintenance reminders |

## Architecture

```
stories/
├── README.md           # This file
├── TODO.md             # Weighted backlog of future stories
├── conftest.py         # Pytest fixtures: story loading, HA container, FastMCP client
├── test_stories.py     # Parametrized test runner
├── run_story.py        # Standalone story runner (container-per-agent, JSONL tracking)
├── scripts/
│   ├── ha_query.py     # Query live HA via agent+MCP (for black-box verification)
│   └── measure_tools.py # Measure tool description sizes across versions
└── catalog/            # Story definitions (YAML)
    ├── s01_automation_sunset_lights.yaml
    ├── s02_automation_motion_light.yaml
    └── ...
```

## Relationship to BAT

Stories build on top of the BAT framework (`tests/uat/run_uat.py`):
- BAT provides the raw agent execution engine (run prompts, collect results)
- Stories provide the **what to test** (realistic use cases with setup and verification)
- `run_story.py` manages containers, setup via FastMCP, and JSONL result tracking with token data
- The `/bat-story-eval` skill orchestrates evaluation: run stories, black-box verify via `ha_query.py`, white-box analyze session files, score, and detect regressions
