# Privacy Policy

**Last updated:** November 2024

## Scope

This privacy policy covers only the Home Assistant MCP Server ("ha-mcp") software. It does not cover the MCP host or client application you use to run ha-mcp. Please refer to your MCP client's privacy policy for information about how it handles your data.

## Overview

Ha-mcp runs on your local machine and communicates with your own Home Assistant instance. We are committed to transparency about any data collection.

## Anonymous Usage Statistics

Ha-mcp may collect anonymous usage statistics to help improve the server. If enabled, this may include:

- **Tool usage counts** — which tools are used and how often
- **Server version** — to understand adoption of updates
- **Request/response sizes** — to optimize performance (not content)

**What we do NOT collect:**
- Entity names or IDs
- Home Assistant configuration
- Personal information
- Automation or script content
- Any data from your smart home devices

If telemetry is enabled, this anonymous data is sent to our server and used solely to improve ha-mcp. You can control this in the configuration settings.

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
- Is NOT collected, stored, or transmitted by ha-mcp (except as described above for anonymous statistics)

## Third-Party Services

Ha-mcp communicates with:

- **Your Home Assistant instance** — via the URL and token you provide
- **Your MCP client** — the application that runs ha-mcp
- **Our telemetry server** — for anonymous usage statistics (if enabled)

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
| Anonymous telemetry | Optional (configurable) |
| Personal data collected | None |
| Bug reports | User-approved only |
| Local processing | Yes |
| Third-party sharing | None (except anonymous stats if enabled) |
