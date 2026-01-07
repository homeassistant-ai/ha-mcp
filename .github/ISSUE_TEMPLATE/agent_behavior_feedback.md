---
name: AI Agent Behavior Feedback
about: Report AI agent inefficiency, wrong tool usage, or suggest workflow improvements
title: '[AGENT] '
labels: agent-behavior, enhancement
assignees: julienld

---

## 🤖 What Did the AI Agent Do?

<!-- Describe what the AI agent did that could be improved -->
<!-- Examples: -->
<!-- - Used the wrong tool initially, then corrected itself -->
<!-- - Provided invalid parameters to a tool -->
<!-- - Made multiple unnecessary tool calls -->
<!-- - Missed an obvious shortcut or better approach -->
<!-- - Misinterpreted tool output -->


## 🎯 What Should the Agent Have Done?

<!-- Describe the more efficient or correct approach -->


## 📝 Conversation Context

<!-- Provide context about what you were trying to do -->
<!-- Example: "I asked the agent to create an automation that..." -->


---

## 🔧 Tool Calls Made

<!-- List the sequence of tools the agent called -->
<!-- You can ask the agent: "Show me the recent tool calls" -->
<!-- Format: -->

1. `ha_tool_name(params)` - Result: ...
2. `ha_other_tool(params)` - Result: ...
3. ...

---

## 💡 Suggested Improvement

<!-- How could the agent be improved? Options: -->

- [ ] **Tool documentation** - Tool description or examples need clarification
- [ ] **Error messages** - Tool should return better guidance on failure
- [ ] **Tool design** - Tool should accept different parameters or return more info
- [ ] **Agent prompting** - System prompt should guide agent differently
- [ ] **New tool needed** - Missing functionality requires a new tool
- [ ] **Other** - Describe below

**Details:**
<!-- Explain your suggestion -->


---

## 📊 Environment (Optional)

<!-- If relevant, provide: -->
- **ha-mcp Version:**
- **AI Client:** (Claude Desktop / Claude Code / Other)
- **Home Assistant Version:**

---

## 📎 Additional Context

<!-- Screenshots, conversation logs, or other helpful info -->


---

**Note:** This is NOT for reporting bugs in ha-mcp itself. This is for improving how AI agents interact with ha-mcp tools.

If you're experiencing a bug (errors, crashes, incorrect behavior), please use the [Runtime Bug](?template=runtime_bug.md) or [Startup Bug](?template=startup_bug.md) template instead.
