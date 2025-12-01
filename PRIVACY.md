# Privacy Policy

**Last updated:** November 2024

## Scope

This privacy policy covers only the Home Assistant MCP Server ("ha-mcp") software. It does not cover the MCP host or client application you use to run ha-mcp. Please refer to your MCP client's privacy policy for information about how it handles your data.

## Overview

Ha-mcp runs on your local machine and communicates with your own Home Assistant instance. We are committed to transparency about any data collection.

## Anonymous Usage Statistics

Ha-mcp does not currently collect any telemetry. In the future, we may add optional anonymous usage statistics to help improve the server. If implemented, this may include:

- **Tool usage counts** — which tools are used and how often
- **Server version** — to understand adoption of updates
- **Request/response sizes** — to optimize performance (not content)

**What we will NOT collect:**
- Entity names or IDs
- Home Assistant configuration
- Personal information
- Automation or script content
- Any data from your smart home devices

If telemetry is added in the future, it will be configurable and this policy will be updated accordingly.

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

Currently, ha-mcp does not communicate with any other external services.

## Data Security

- Your Home Assistant credentials are stored locally by your MCP client
- Anonymous telemetry contains no identifying information
- Bug reports are only sent when you explicitly approve

## Changes to This Policy

We may update this privacy policy to reflect changes in our practices. Significant changes will be noted in release notes.

## Contact

For privacy-related questions or concerns:

- **GitHub Issues:** [https://github.com/homeassistant-ai/ha-mcp/issues](https://github.com/homeassistant-ai/ha-mcp/issues)
- **Email:** github@qc-h.net

## Summary

| Aspect | Status |
|--------|--------|
| Anonymous telemetry | Not implemented (may be added in future) |
| Personal data collected | None |
| Bug reports | User-approved only |
| Local processing | Yes |
| Third-party data sharing | None |
