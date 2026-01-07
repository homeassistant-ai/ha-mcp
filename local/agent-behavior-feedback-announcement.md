# 🤖 New: AI Agent Behavior Feedback Template

**Date**: 2026-01-07

## What's New?

We've added a **new way to help improve how AI agents interact with ha-mcp**: the **Agent Behavior Feedback** template!

## What is it for?

This is **NOT** for reporting bugs in ha-mcp itself. Instead, use it to report when:

- 🔄 **The AI agent used the wrong tool initially**, then corrected itself
- ❌ **The agent provided invalid parameters** to a tool
- 🔁 **The agent made multiple unnecessary tool calls** to accomplish something
- 🎯 **The agent missed an obvious shortcut** or better approach
- 🤔 **The agent misinterpreted tool output** or documentation

## Why does this matter?

When you report these behaviors, we can:

1. **Improve tool descriptions** - Make it clearer which tool to use when
2. **Better error messages** - Guide the agent toward the right solution
3. **Tool design improvements** - Add parameters or return values that help the agent work more efficiently
4. **Documentation** - Add examples that prevent common mistakes

## How to use it

1. **Notice inefficient behavior** - Did the agent struggle or take a roundabout path?
2. **File feedback**: Go to [github.com/homeassistant-ai/ha-mcp/issues/new](https://github.com/homeassistant-ai/ha-mcp/issues/new) and choose **"AI Agent Behavior Feedback"**
3. **Describe what happened** - What did the agent do? What should it have done?
4. **Provide tool call sequence** - Ask the agent: "Show me the recent tool calls"

## Examples of good feedback

### Example 1: Wrong tool, then correction
**What happened:**
```
Agent first called ha_get_state("light.bedroom")
Got error about entity not found
Then called ha_search_entities("bedroom light")
Found the correct entity: light.bedroom_main
```

**Feedback:** The agent should use `ha_search_entities` FIRST when the exact entity ID is unknown, instead of guessing and failing with `ha_get_state`.

### Example 2: Invalid parameters
**What happened:**
```
Agent called ha_config_set_automation with:
{
  "trigger": "sunset",  # Should be an array of trigger dicts
  "action": "turn on light"  # Should be an array of action dicts
}
Got validation error
```

**Feedback:** Tool description should clarify that trigger/action must be ARRAYS, not strings. Add an example showing the correct structure.

### Example 3: Missing obvious tool
**What happened:**
```
Agent called ha_list_services to see all services
Then manually searched through the output for "light" domain services
```

**Feedback:** Should we add a `ha_list_services_for_domain("light")` tool to avoid the agent having to filter manually?

## What happens next?

We review all feedback and prioritize improvements that will have the biggest impact on agent efficiency. Common issues get fixed quickly!

## Link to template

https://github.com/homeassistant-ai/ha-mcp/issues/new?template=agent_behavior_feedback.md

---

**Remember**: For actual bugs (errors, crashes, incorrect behavior in ha-mcp), use the [Runtime Bug](https://github.com/homeassistant-ai/ha-mcp/issues/new?template=runtime_bug.md) or [Startup Bug](https://github.com/homeassistant-ai/ha-mcp/issues/new?template=startup_bug.md) templates instead!

---

## Also New: Improved Bug Report Templates

We've also split the bug report template into two specialized versions:

### 🏃 Runtime Bug Template
For bugs that occur while ha-mcp is running. Use the `ha_bug_report` tool to auto-collect:
- Environment info
- Recent tool calls
- Error messages
- Startup logs (if relevant)

**Link**: https://github.com/homeassistant-ai/ha-mcp/issues/new?template=runtime_bug.md

### 🚀 Startup/Installation Bug Template
For bugs preventing ha-mcp from starting or installing. Fill manually since the server isn't running.

**Link**: https://github.com/homeassistant-ai/ha-mcp/issues/new?template=startup_bug.md

---

## Share Your Experience!

Have you noticed your AI agent struggling with ha-mcp? **We want to hear about it!** Your feedback helps make ha-mcp better for everyone.

File feedback at: https://github.com/homeassistant-ai/ha-mcp/issues/new?template=agent_behavior_feedback.md
