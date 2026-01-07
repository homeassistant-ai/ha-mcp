---
name: AI Agent Behavior Feedback
about: Report AI agent inefficiency, wrong tool usage, or suggest workflow improvements
title: '[AGENT] '
labels: agent-behavior, enhancement
assignees: julienld

---

## 💡 Use the `ha_report_issue` Tool!

> **Ask your AI assistant:** *"You should have used a different tool"* or *"That was inefficient"*
>
> The agent will call `ha_report_issue()` which auto-collects tool call history and generates a template.
>
> The agent will automatically determine if this is agent behavior feedback vs a bug report.

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

<!-- If you used ha_report_issue, the tool call sequence is auto-filled below -->

<details>
<summary>Click to expand tool call sequence</summary>

```
<!-- Sequence of tools the agent called -->
<!-- Format: timestamp | tool_name | OK/FAIL | exec_time -->
<!-- ha_report_issue automatically collects this -->
```

</details>

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
