# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| latest  | ✅        |

## Scope

**In scope** — please report these:

- Authentication bypass in standard (LLAT) or OAuth mode
- OAuth mode: XSS, SSRF, token leakage, open redirect
- Prompt injection paths that circumvent tool-level safeguards
  (e.g., HA entity data triggering unintended tool calls)
- Privilege escalation within the MCP tool surface
- Sandbox escape via sandboxed code execution, if that feature is enabled
- Unintended information disclosure via API responses
- Dependency vulnerabilities with a credible exploit path

**Out of scope** — these will not be actioned:

- Vulnerabilities in Home Assistant itself →
  report to [home-assistant/core](https://github.com/home-assistant/core/security)
- Vulnerabilities in Nabu Casa or other remote access infrastructure
- Attacks requiring physical access to the HA host
- "The LLM performed a destructive action using valid, authorized tools" —
  this is a configuration or usage issue, not a security vulnerability.
  Tool visibility controls (`DISABLED_TOOLS`, group toggles) exist for this purpose.
- Vulnerabilities that are only exploitable due to a misconfigured deployment
  (e.g., standard-mode instance exposed to the internet without TLS)

## OAuth Mode — Beta Warning

The OAuth consent-flow mode (`ha-mcp-oauth` entrypoint) is **experimental**
and carries a larger attack surface than the standard LLAT setup.

- Not recommended for production without TLS and network access restrictions
- Requires explicit opt-in (`ha-mcp-oauth`); the default entrypoint is unaffected
- Two CVEs were published and fixed in v7.x (XSS: GHSA-pf93-j98v-25pv;
  SSRF: GHSA-fmfg-9g7c-3vq7). Upgrade to the latest release before deploying.

If you choose to run OAuth mode, restrict the consent endpoint to trusted networks
and place it behind a TLS-terminating reverse proxy.

## Reporting a Vulnerability

Use the private reporting page at:
**https://github.com/homeassistant-ai/ha-mcp/security/advisories/new**

Reports are assessed within 48 hours; fixes may take an additional 24–48 hours. We aim for coordinated disclosure and will work with you to agree on a disclosure timeline, typically within 90 days of the initial report.
Severity is assessed using CVSS base scores where applicable.

**Requirements for a valid report:**
- Reports must be made in good faith
- Demonstrate a real, reproducible issue with steps to reproduce
- Accurately reflect severity and impact — overstated reports are deprioritized
- Low-quality or AI-generated submissions without a working proof of concept
  will be closed without action
