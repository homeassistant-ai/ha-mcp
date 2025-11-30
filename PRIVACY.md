# Privacy Policy

**Last updated:** November 2024

## Overview

Home Assistant MCP Server ("ha-mcp") is designed with privacy as a core principle. The server runs entirely on your local machine and communicates only with your own Home Assistant instance.

## Data Collection

### What We Collect

**Currently, we collect no data.** Ha-mcp operates entirely locally:

- No telemetry or analytics are sent to external servers
- No usage data is collected or transmitted
- No personal information is gathered
- All communication occurs directly between Claude Desktop and your local Home Assistant instance

### Your Home Assistant Data

When you use ha-mcp, Claude Desktop may access data from your Home Assistant instance, including:

- Entity states and attributes
- Device information
- Automation and script configurations
- Historical data and statistics
- Camera snapshots (when requested)

This data is processed locally by Claude Desktop and is subject to [Anthropic's Privacy Policy](https://www.anthropic.com/privacy). Ha-mcp itself does not store, log, or transmit this data to any third party.

## Future Analytics

We may introduce **optional, anonymized usage analytics** in future versions to help improve the server. Any such data collection will:

- Be strictly **opt-in** with clear disclosure
- Collect only anonymized, aggregated usage patterns (e.g., which tools are most used)
- Never include personal information, entity names, or Home Assistant data
- Be clearly documented before activation

You will always have the choice to use ha-mcp without any data collection.

## Third-Party Services

Ha-mcp does not integrate with or send data to any third-party services. The only external communication is:

- **Your Home Assistant instance** - via the URL and token you provide
- **Claude Desktop** - the MCP client that runs ha-mcp

## Data Security

Since ha-mcp runs locally and collects no data:

- Your Home Assistant credentials (URL and token) are stored locally by Claude Desktop
- No data is transmitted to ha-mcp developers or any external servers
- All processing occurs on your machine

## Children's Privacy

Ha-mcp is a technical tool intended for smart home management. We do not knowingly collect any information from children.

## Changes to This Policy

We may update this privacy policy to reflect changes in our practices or for legal reasons. Significant changes will be noted in release notes.

## Contact

For privacy-related questions or concerns:

- **GitHub Issues:** [https://github.com/homeassistant-ai/ha-mcp/issues](https://github.com/homeassistant-ai/ha-mcp/issues)
- **Email:** github@qc-h.net

## Summary

| Aspect | Status |
|--------|--------|
| Data collected | None |
| External transmission | None |
| Third-party sharing | None |
| Local processing only | Yes |
| Future analytics | Opt-in only |
