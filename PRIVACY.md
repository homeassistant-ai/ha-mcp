# Privacy Policy

**Last updated:** June 2026

## Scope

This privacy policy covers only the Home Assistant MCP Server ("ha-mcp") software. It does not cover the MCP host or client application you use to run ha-mcp. Please refer to your MCP client's privacy policy for information about how it handles your data.

## Overview

Ha-mcp runs on your local machine and communicates with your own Home Assistant instance. We are committed to transparency about any data collection.

## Anonymous Usage Statistics

Anonymous usage statistics are a planned future feature and are **not currently collected or transmitted** as of June 2026. When this feature is implemented, it will respect your Home Assistant analytics/telemetry setting by default, and you will be able to override that choice (opt out). The change will be announced prominently in the release notes — shown in the README, on the GitHub releases page, and in the web Settings UI — at least one month before it takes effect.

When enabled in a future release, anonymous usage statistics would include:

- **Tool usage counts** — which tools are used and how often
- **Server version** — to understand adoption of updates
- **Request/response sizes** — to optimize performance (not content)

**What would NOT be collected:**
- Entity names or IDs
- Home Assistant configuration
- Personal information
- Automation or script content
- Any data from your smart home devices

## Bug Reports

Ha-mcp may include a bug reporting feature that allows you to send diagnostic information when you encounter issues. Bug reports are:

- **Only sent with your explicit approval** — the AI assistant will ask before sending
- **Reviewed with you first** — you'll see what information is included
- **Anonymized** — personal data should be replaced with generic values before submission

You are always in control of whether to send a bug report.

## Your Home Assistant Data

When you use ha-mcp, your MCP client accesses data from your Home Assistant instance, including entity states, automations, and device information. This data:

- Is processed by your MCP client application
- Is subject to your MCP client's privacy policy
- Is NOT collected, stored, or transmitted by ha-mcp

## Services Ha-mcp Communicates With

- **Your Home Assistant instance** — via the URL and token you provide
- **Your MCP client** — the application that runs ha-mcp
- **A telemetry server** — *planned future feature, not currently active*; when active it would follow your Home Assistant analytics/telemetry setting, which you can override (opt out)

## Data Security

- Your Home Assistant credentials are stored locally by your MCP client
- Bug reports are only sent when you explicitly approve
- Anonymous telemetry (when implemented) will contain no identifying information

## Changes to This Policy

We may update this privacy policy to reflect changes in our practices. Any change that affects what data is collected or transmitted will be announced prominently in the release notes — shown in the README, on the GitHub releases page, and in the web Settings UI — at least one month before it takes effect.

## Contact

For privacy-related questions or concerns:

- **GitHub Issues:** [https://github.com/homeassistant-ai/ha-mcp/issues](https://github.com/homeassistant-ai/ha-mcp/issues)
- **Email:** github@qc-h.net

## Summary

| Aspect | Status |
|--------|--------|
| Anonymous telemetry | Not currently implemented (planned; will follow your Home Assistant analytics/telemetry setting, override-able) |
| Personal data collected | None |
| Bug reports | User-approved only |
| Local processing | Yes |
| Third-party data sharing | None |
