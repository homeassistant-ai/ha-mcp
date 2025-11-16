# YAML vs JSON for Dashboard Configs - Analysis

## Question
Should we support YAML strings, JSON, or both for dashboard configurations? Is there a conversion algorithm?

## Answer: Support Both (Like Current Architecture)

### Home Assistant's Behavior

**WebSocket API Schema:**
```python
# From lovelace/websocket.py
{
    "type": "lovelace/config/save",
    "config": vol.Any(str, dict),  # Accepts BOTH string OR dict
}
```

**Backend Storage:**
- Internally stores as **JSON** (dict) in `.storage/lovelace*` files
- WebSocket accepts either string or dict
- If string is provided, HA parses it (likely as YAML, but could be JSON)

### Current MCP Architecture Pattern

**We already handle this in existing tools!**

From `util_helpers.py`:
```python
def parse_json_param(param: str | dict | list | None) -> dict | list | None:
    """Parse flexibly JSON string or return existing dict/list."""
    if isinstance(param, (dict, list)):
        return param  # Already correct type

    if isinstance(param, str):
        parsed = json.loads(param)  # Parse as JSON
        return parsed
```

**Current usage** (scripts, automations, helpers):
- Tool parameter: `config: str | dict[str, Any]`
- We call `parse_json_param(config)` to normalize
- Currently only handles **JSON strings**, not YAML

### YAML ↔ JSON Conversion

**Good news: YAML is a superset of JSON**
- All valid JSON is valid YAML
- YAML supports additional features (anchors, multi-line, comments)

**Python conversion:**
```python
import yaml
import json

# YAML → Python dict
config_dict = yaml.safe_load(yaml_string)

# Python dict → JSON string
json_string = json.dumps(config_dict, indent=2)

# Python dict → YAML string
yaml_string = yaml.dump(config_dict, default_flow_style=False)
```

**Issues to consider:**
1. **YAML features lost in JSON**: anchors (`&ref`, `*ref`), comments
2. **JSON is subset**: Always safe to convert YAML → dict → JSON
3. **Indentation**: YAML is whitespace-sensitive

## Recommendation: Support Both (Like Current Tools)

### Option 1: JSON Only (Current State)

**Pros:**
- ✅ Already implemented (`parse_json_param`)
- ✅ No new dependencies
- ✅ FastMCP/JSON-RPC native format
- ✅ AI models excel at generating JSON

**Cons:**
- ❌ Users familiar with YAML configs can't use YAML
- ❌ Multi-line strings awkward in JSON
- ❌ Less human-readable for large configs

### Option 2: Support Both (Recommended)

**Pros:**
- ✅ Matches Home Assistant WebSocket API behavior
- ✅ Flexible for users (JSON or YAML)
- ✅ AI can generate either format
- ✅ Consistency with HA patterns

**Cons:**
- ⚠️ Need YAML parser (PyYAML already available in env)
- ⚠️ Slightly more complex parsing logic

### Option 3: YAML Only

**Pros:**
- ✅ More Home Assistant-native
- ✅ Better for human-readable configs

**Cons:**
- ❌ Less natural for MCP/JSON-RPC protocol
- ❌ AI models may make indentation errors
- ❌ Requires YAML parsing

## Implementation Recommendation

### Update `util_helpers.py`

Add YAML support to existing parser:

```python
import json
import yaml
from typing import Any


def parse_config_param(
    param: str | dict | list | None, param_name: str = "config"
) -> dict | list | None:
    """
    Parse configuration from JSON/YAML string or return existing dict/list.

    Supports three input formats:
    1. Dict/list (passthrough)
    2. JSON string (parsed with json.loads)
    3. YAML string (parsed with yaml.safe_load)

    Args:
        param: Config as dict, list, JSON string, or YAML string
        param_name: Parameter name for error messages

    Returns:
        Parsed dict/list or None

    Raises:
        ValueError: If parsing fails or wrong type

    Examples:
        # Dict passthrough
        parse_config_param({"views": []}) → {"views": []}

        # JSON string
        parse_config_param('{"views": []}') → {"views": []}

        # YAML string
        parse_config_param('views:\\n  - title: Home') → {"views": [{"title": "Home"}]}
    """
    if param is None:
        return None

    if isinstance(param, (dict, list)):
        return param

    if isinstance(param, str):
        # Try JSON first (faster, more common for MCP)
        try:
            parsed = json.loads(param)
            if not isinstance(parsed, (dict, list)):
                raise ValueError(
                    f"{param_name} must be object/array, got {type(parsed).__name__}"
                )
            return parsed
        except json.JSONDecodeError:
            # Fall back to YAML
            try:
                parsed = yaml.safe_load(param)
                if not isinstance(parsed, (dict, list)):
                    raise ValueError(
                        f"{param_name} must be object/array, got {type(parsed).__name__}"
                    )
                return parsed
            except yaml.YAMLError as e:
                raise ValueError(f"Invalid JSON/YAML in {param_name}: {e}")

    raise ValueError(
        f"{param_name} must be string, dict, list, or None, got {type(param).__name__}"
    )


# Keep backwards compatibility alias
parse_json_param = parse_config_param
```

### Usage in Dashboard Tools

```python
@mcp.tool
async def ha_config_update_dashboard(
    url_path: str,
    config: Annotated[
        str | dict[str, Any],
        Field(
            description="Dashboard configuration as dict, JSON string, or YAML string. "
            "Both formats are supported."
        )
    ]
):
    """
    Update dashboard configuration.

    Config can be provided as:
    - Python dict (recommended for MCP)
    - JSON string: '{"views": [...]}'
    - YAML string: 'views:\\n  - title: Home'

    EXAMPLES:

    JSON format:
    ha_config_update_dashboard("mobile", {
        "views": [{
            "title": "Home",
            "cards": [{"type": "weather-forecast"}]
        }]
    })

    YAML format (as string):
    ha_config_update_dashboard("mobile", '''
    views:
      - title: Home
        cards:
          - type: weather-forecast
    ''')
    """
    config_dict = parse_config_param(config, "config")
    await client.websocket_client.save_dashboard_config(config_dict, url_path)
    # ...
```

## Storage Format (Backend)

**Home Assistant stores as JSON:**
```json
// .storage/lovelace
{
  "version": 1,
  "key": "lovelace",
  "data": {
    "config": {
      "views": [
        {"title": "Home", "cards": [...]}
      ]
    }
  }
}
```

**When we retrieve:**
- WebSocket returns Python dict
- We return as JSON to MCP client
- User can convert to YAML if desired

## Conclusion

### Recommended Approach

✅ **Support both JSON and YAML strings** (like HA WebSocket API)

**Implementation:**
1. Add `parse_config_param()` with YAML fallback
2. Keep `parse_json_param` as alias for backwards compatibility
3. Document both formats in tool docstrings
4. Default examples use dict/JSON (more MCP-native)
5. Add YAML examples for users familiar with HA configs

**Dependencies:**
- PyYAML already available (Python stdlib includes yaml module)
- No new dependencies needed

**Benefits:**
- ✅ Maximum flexibility
- ✅ Matches HA behavior
- ✅ AI can use JSON (easier for models)
- ✅ Users can paste YAML from HA UI
- ✅ Graceful fallback (JSON first, then YAML)

## Example Tool Documentation

```python
config: Annotated[
    str | dict[str, Any],
    Field(
        description="Dashboard configuration. Supports dict, JSON string, or YAML string. "
        "JSON is recommended for programmatic use, YAML for human-readability."
    )
]
```

This follows the principle: **Be liberal in what you accept, conservative in what you send**.
