# OIDC Authelia Scope and Diagnostics Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the standalone/container `ha-mcp-oidc` entrypoint work with Authelia's default opaque access tokens by always requesting `openid`, preserve explicitly requested optional OIDC scopes and pre-fix DCR registrations across the upgrade, make `LOG_LEVEL` control FastMCP's OIDC diagnostics, and document the required configuration.

**Architecture:** Wrap FastMCP 3.4.6 `OIDCProxy` in a narrow compatibility subclass with the inherited constructor signature. Supply protocol-level `openid` through `required_scopes`, then explicitly remove only FastMCP's DCR valid-scope allow-list while retaining its `openid` required/default/advertised state. Retain explicitly requested optional scopes, add `openid` to optional-only registrations, and independently union it into every authorization request before proxying upstream. Let the upstream IdP validate requested optional scopes, lazily migrate persisted registrations that lack `openid`, and reject legacy refresh tokens that lack it so clients reauthorize once without losing DCR state. Retain opt-in ID-token verification for opaque access-token providers, and bridge ha-mcp's common logging level into FastMCP's non-propagating logger without replacing FastMCP's handlers while preserving `FASTMCP_LOG_ENABLED=false`.

**Tech Stack:** Python 3.13, FastMCP 3.4.6 `OIDCProxy`, MCP Python SDK 1.28.1, pytest/pytest-asyncio, Ruff, mypy, Markdown, GitHub CLI.

## Global Constraints

- Work only in `/data/data/com.termux/files/home/ha-mcp/worktree/fix-oidc-authelia` on branch `agent/fix-oidc-authelia`; never modify or commit from `master` or the user's dirty main checkout.
- The OIDC scope and persisted-state compatibility behavior applies only to the standalone/container `ha-mcp-oidc` entrypoint; do not alter the Home Assistant add-on, custom component, OAuth mode, audience/resource forwarding, redirect URI policy, or token endpoint authentication.
- The FastMCP logger-level bridge and `FASTMCP_LOG_ENABLED=false` protection are intentionally common behavior for every ha-mcp entrypoint that calls `_setup_logging`; they do not change those entrypoints' authentication models.
- Always pass exactly `required_scopes=["openid"]`; do not make OIDC scopes configurable because `openid` is mandatory for this OIDC entrypoint.
- Keep `openid` as the only required, default, and protected-resource-advertised scope. Do not copy every provider discovery scope into DCR metadata.
- Permit DCR clients to explicitly request optional OIDC scopes, retain those scopes while adding missing `openid`, union `openid` into every authorization request, and leave final optional-scope authorization to the upstream IdP.
- Preserve existing DCR registrations: lazily add missing required scopes and persist only changed records. Reject legacy refresh tokens without `openid` so the client reauthorizes once; manual client cache clearing is only a fallback when automatic reauthorization does not start.
- Keep `OIDC_VERIFY_ID_TOKEN` opt-in: JWT access-token providers retain default verification, while Authelia and other opaque-access-token providers set it to `true`.
- Preserve FastMCP's handlers and formatting; when FastMCP logging is enabled, only set the `fastmcp` logger's level to the validated ha-mcp `LOG_LEVEL` value. When `FASTMCP_LOG_ENABLED=false`, keep its otherwise propagating namespace above `CRITICAL` so it cannot inherit ha-mcp's root handler.
- Do not require Authelia JWT access tokens or add introspection support.
- Follow red-green TDD for both behavior changes, use `apply_patch` for source edits, and verify the branch and worktree before every commit.
- Run Python setup and verification in the existing Ubuntu `proot-distro` environment with `UV_LINK_MODE=copy`; run pytest from the repository's `tests/` directory.
- Create the GitHub pull request as a draft and do not mark it ready for review without explicit user approval.

## File Map

- `src/ha_mcp/auth/oidc_compat.py`: FastMCP 3.4.6 compatibility subclass for scope setup, DCR and authorization normalization, persisted-client migration, and refresh-token scope enforcement.
- `tests/src/unit/test_oidc_compat.py`: focused real-provider tests for inherited constructor compatibility, DCR defaults and optional scopes, protected-resource metadata, upstream authorization scopes, persisted-client migration, and refresh-token rejection.
- `tests/src/unit/test_oidc_entrypoint.py`: pinned proxy-construction contract plus regression coverage for the required OIDC scope, explicit compatibility setup call, enabled FastMCP logger level, and disabled FastMCP logging behavior.
- `src/ha_mcp/__main__.py`: common logging setup and standalone/container OIDC proxy construction.
- `docs/oidc.md`: operator guidance for scope behavior, state migration and reauthorization, Authelia's opaque tokens, ID-token verification, debug logging, and the FastMCP logging opt-out.
- `docs/superpowers/specs/2026-08-10-oidc-authelia-scope-design.md`: approved scope/state compatibility behavior, logging opt-out, and non-goals.

---

### Task 1: Require `openid` without blocking optional scopes or legacy clients

**Files:**
- Create: `tests/src/unit/test_oidc_compat.py`
- Create: `src/ha_mcp/auth/oidc_compat.py`
- Modify: `tests/src/unit/test_oidc_entrypoint.py`
- Modify: `src/ha_mcp/__main__.py`

**Interfaces:**
- Consumes: FastMCP 3.4.6 `OIDCProxy`, its DCR stores and token loaders, and MCP Python SDK registration and protected-resource-metadata handlers.
- Produces: `HaMcpOIDCProxy`, which preserves the inherited constructor signature; an explicit `setup_scope_compatibility() -> None`; DCR and authorization-request scope normalization; lazy persisted-client migration; and required-scope refresh-token validation.
- Produces: `_run_oidc_server(...) -> None` constructs the compatibility proxy with `required_scopes=["openid"]` and calls setup exactly once before attaching it to the MCP server.

- [ ] **Step 1: Verify the isolated branch and baseline**

Run from the worktree:

```bash
pwd
git rev-parse --abbrev-ref HEAD
git status --short --branch
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia && UV_LINK_MODE=copy uv sync --group dev'
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia/tests && HA_MCP_CONFIG_DIR=/tmp/ha-mcp-2189-baseline UV_LINK_MODE=copy uv run pytest src/unit/test_oidc_entrypoint.py -q'
```

Expected: the branch and worktree are correct, dependency sync succeeds, and the untouched entrypoint unit file passes. Distinguish any environment failure from issue #2189 before changing production code.

- [ ] **Step 2: Create the focused compatibility tests first and capture import red**

Create `tests/src/unit/test_oidc_compat.py` with a real `OIDCConfiguration`,
`MemoryStore`, and `RegistrationHandler`; replace only external discovery. Import
`HaMcpOIDCProxy`, then run:

```bash
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia/tests && HA_MCP_CONFIG_DIR=/tmp/ha-mcp-2189-red-import UV_LINK_MODE=copy uv run pytest src/unit/test_oidc_compat.py --collect-only -q'
```

Expected: collection fails with `ModuleNotFoundError` for
`ha_mcp.auth.oidc_compat`. This proves the compatibility layer does not already
exist.

- [ ] **Step 3: Add the subclass shell and capture the scope-setup red state**

Create `src/ha_mcp/auth/oidc_compat.py` with `HaMcpOIDCProxy(OIDCProxy)` and a
no-op explicit setup method, without overriding `__init__`. Add a signature
assertion and a setup test that starts from FastMCP's
`valid_scopes == ["openid"]`, invokes setup, and requires all of:

```python
assert proxy.required_scopes == ["openid"]
assert proxy.client_registration_options.default_scopes == ["openid"]
assert proxy._default_scope_str == "openid"
assert proxy.client_registration_options.valid_scopes is None
```

Run the setup test and require it to fail because `valid_scopes` remains
`["openid"]`.

- [ ] **Step 4: Add focused DCR, metadata, persisted-state, and refresh tests**

Before implementing the methods, add behavioral tests proving:

- explicit `openid profile email offline_access` DCR succeeds;
- omitted DCR scope defaults to `openid`;
- optional-only DCR retains its requested scopes and adds `openid`;
- `/.well-known/oauth-protected-resource/mcp` advertises only `openid`;
- an optional-only authorization request produces an upstream IdP URL whose
  `scope` contains both `openid` and the requested optional scopes;
- a persisted `profile email` client is migrated to `openid profile email` and
  written exactly once across repeated loads;
- a compliant persisted client keeps its scope order and is not rewritten;
- a stored refresh token without `openid` is rejected; and
- a stored refresh token with `openid` remains usable.

Run the focused file and retain the expected failures from the unimplemented
scope setup, registration normalization, authorization normalization,
migration, and refresh checks.

- [ ] **Step 5: Implement the narrow FastMCP 3.4.6 compatibility layer**

Implement `setup_scope_compatibility()` so it first verifies exactly
`required_scopes == ["openid"]`, DCR `default_scopes == ["openid"]`, and
`_default_scope_str == "openid"`, then sets only
`client_registration_options.valid_scopes = None`. `None` removes the MCP SDK's
local complete allow-list while protected-resource metadata continues to fall
back to required `openid`; the upstream IdP still validates every explicitly
requested optional scope.

Normalize `register_client()` so explicitly requested optional scopes are
retained and any missing required scope is added before FastMCP persists the
registration. Normalize `authorize()` independently so every authorization
transaction and upstream IdP request unions required `openid` into the client's
requested scopes; do not rely on registration normalization alone.

Override `get_client()` to detect an already-persisted client, add missing
required scopes while preserving the existing optional-scope order, and write
the migrated record only when it changed. Override `load_refresh_token()` to
return `None` when FastMCP loads a token that lacks any required scope. Retain
the DCR registration so the rejected refresh triggers authorization, not a new
client registration.

- [ ] **Step 6: Run the focused compatibility file and verify green**

Run:

```bash
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia/tests && HA_MCP_CONFIG_DIR=/tmp/ha-mcp-2189-green-compat UV_LINK_MODE=copy uv run pytest src/unit/test_oidc_compat.py -q'
```

Expected: every constructor, DCR/default/optional-only, metadata, upstream
authorization, persistence, and refresh-token test passes.

- [ ] **Step 7: Integrate the compatibility proxy into the OIDC entrypoint**

Update the pinned proxy mock to require and record `required_scopes`, expose a
`setup_scope_compatibility` spy, and patch
`ha_mcp.auth.oidc_compat.HaMcpOIDCProxy`. Assert construction receives exactly
`["openid"]` and setup is called once. In `_run_oidc_server`, import and
construct `HaMcpOIDCProxy`, keep the existing optional constructor kwargs, pass
`required_scopes=["openid"]`, and call setup immediately after construction.

- [ ] **Step 8: Run the entrypoint and compatibility regressions together**

Run:

```bash
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia/tests && HA_MCP_CONFIG_DIR=/tmp/ha-mcp-2189-green-scope UV_LINK_MODE=copy uv run pytest src/unit/test_oidc_compat.py src/unit/test_oidc_entrypoint.py::TestRunOidcServer src/unit/test_oidc_entrypoint.py::TestOIDCProxySignatureSubset -q'
```

Expected: both the real compatibility behavior and the entrypoint integration pass.

- [ ] **Step 9: Verify location and branch, then commit the scope compatibility change**

Run:

```bash
pwd
git rev-parse --abbrev-ref HEAD
git diff --check
git add src/ha_mcp/auth/oidc_compat.py src/ha_mcp/__main__.py tests/src/unit/test_oidc_compat.py tests/src/unit/test_oidc_entrypoint.py
git commit -m "fix: preserve OIDC scope compatibility"
```

Expected: a commit on `agent/fix-oidc-authelia`, never on `master` or `main`.

---

### Task 2: Make `LOG_LEVEL` control enabled FastMCP diagnostics

**Files:**
- Modify: `tests/src/unit/test_oidc_entrypoint.py:7-10,187-269`
- Modify: `src/ha_mcp/__main__.py:485-518`

**Interfaces:**
- Consumes: `_setup_logging(log_level_str: str, force: bool = True) -> None` and Python's `logging.getLogger("fastmcp")`.
- Produces: `_setup_logging` sets both the root configuration and an enabled `fastmcp` logger to the same numeric level while leaving FastMCP's handler and `propagate` configuration untouched; when FastMCP logging is disabled, its namespace remains silent instead of inheriting the root handler.

- [ ] **Step 1: Write the failing FastMCP logging regression test**

Add `import logging` with the standard-library imports at the top of `tests/src/unit/test_oidc_entrypoint.py`:

```python
import logging
import os
```

Add this test to `TestMainOidcLogging`:

```python
    def test_setup_logging_configures_fastmcp_logger(self, monkeypatch):
        """LOG_LEVEL should apply to FastMCP's non-propagating logger."""
        import fastmcp
        import ha_mcp.__main__ as main_module

        fastmcp_logger = logging.getLogger("fastmcp")
        streamable_http_logger = logging.getLogger("mcp.server.streamable_http")
        fastmcp_server_logger = logging.getLogger("fastmcp.server.server")
        original_level = fastmcp_logger.level
        original_handlers = fastmcp_logger.handlers[:]
        original_propagate = fastmcp_logger.propagate
        original_formatters = [handler.formatter for handler in original_handlers]
        original_streamable_http_filters = streamable_http_logger.filters[:]
        original_fastmcp_server_filters = fastmcp_server_logger.filters[:]
        try:
            monkeypatch.setattr(fastmcp.settings, "log_enabled", True)
            fastmcp_logger.setLevel(logging.INFO)
            main_module._setup_logging("DEBUG", force=False)
            assert fastmcp_logger.getEffectiveLevel() == logging.DEBUG
            assert fastmcp_logger.handlers == original_handlers
            assert fastmcp_logger.propagate is original_propagate
            assert all(
                handler.formatter is formatter
                for handler, formatter in zip(
                    fastmcp_logger.handlers, original_formatters, strict=True
                )
            )
        finally:
            fastmcp_logger.setLevel(original_level)
            fastmcp_logger.handlers[:] = original_handlers
            fastmcp_logger.propagate = original_propagate
            for handler, formatter in zip(
                original_handlers, original_formatters, strict=True
            ):
                handler.setFormatter(formatter)
            streamable_http_logger.filters[:] = original_streamable_http_filters
            fastmcp_server_logger.filters[:] = original_fastmcp_server_filters
```

Use `force=False` so the test does not replace pytest's root handlers; the behavior under test is the explicit FastMCP logger-level bridge.

- [ ] **Step 2: Run the logging regression test and verify the red state**

Run:

```bash
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia/tests && HA_MCP_CONFIG_DIR=/tmp/ha-mcp-2189-red-logging UV_LINK_MODE=copy uv run pytest src/unit/test_oidc_entrypoint.py::TestMainOidcLogging::test_setup_logging_configures_fastmcp_logger -q'
```

Expected: FAIL because the FastMCP logger remains at `logging.INFO` (`20`) rather than `logging.DEBUG` (`10`).

- [ ] **Step 3: Write and run the disabled-logging regression test**

Add a behavioral regression test that reproduces FastMCP's fresh
`FASTMCP_LOG_ENABLED=false` state (no handlers, `propagate=True`, namespace
level `NOTSET`), calls `_setup_logging`, emits a `CRITICAL` record from
`fastmcp.server.server`, and proves the record does not reach the root capture
handler. Run it before the production change and require the sentinel to appear.

- [ ] **Step 4: Implement the minimum common logging bridge**

Resolve the numeric level once and reuse it in `_setup_logging`:

```python
    log_level = getattr(logging, log_level_str)

    import fastmcp

    from ha_mcp.utils.usage_logger import preserve_startup_collector

    with preserve_startup_collector():
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s %(name)s %(levelname)s: %(message)s",
            datefmt=_LOG_DATE_FORMAT,
            force=force,
        )

    fastmcp_logger = logging.getLogger("fastmcp")
    if fastmcp.settings.log_enabled:
        # FastMCP configures its own handler and disables propagation, so the root
        # level above does not control its OIDC diagnostics. Preserve that handler
        # and formatting while honoring ha-mcp's LOG_LEVEL setting.
        fastmcp_logger.setLevel(log_level)
    else:
        # FastMCP deliberately leaves its logger unconfigured when logging is
        # disabled. Prevent that NOTSET namespace from inheriting our root handler.
        fastmcp_logger.setLevel(logging.CRITICAL + 1)
```

Leave the existing `mcp.server.streamable_http` and `fastmcp.server.server` filters immediately after this block.

- [ ] **Step 5: Run both logging regression tests and verify green**

Run:

```bash
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia/tests && HA_MCP_CONFIG_DIR=/tmp/ha-mcp-2189-green-logging UV_LINK_MODE=copy uv run pytest src/unit/test_oidc_entrypoint.py::TestMainOidcLogging::test_setup_logging_configures_fastmcp_logger src/unit/test_oidc_entrypoint.py::TestMainOidcLogging::test_setup_logging_preserves_fastmcp_logging_opt_out -q'
```

Expected: `2 passed`.

- [ ] **Step 6: Run all logging and OIDC proxy construction tests**

Run:

```bash
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia/tests && HA_MCP_CONFIG_DIR=/tmp/ha-mcp-2189-green-focused UV_LINK_MODE=copy uv run pytest src/unit/test_oidc_entrypoint.py::TestMainOidcLogging src/unit/test_oidc_entrypoint.py::TestRunOidcServer src/unit/test_oidc_entrypoint.py::TestOIDCProxySignatureSubset -q'
```

Expected: all selected tests pass.

- [ ] **Step 7: Verify location and branch, then commit the logging regression and fix**

Run:

```bash
pwd
git rev-parse --abbrev-ref HEAD
git diff --check
git add src/ha_mcp/__main__.py tests/src/unit/test_oidc_entrypoint.py
git commit -m "fix: apply log level to FastMCP"
```

Expected: a second atomic implementation commit on `agent/fix-oidc-authelia`.

---

### Task 3: Document Authelia's working OIDC configuration

**Files:**
- Modify: `docs/oidc.md:1-117`
- Modify: `src/ha_mcp/__main__.py:1618-1625`

**Interfaces:**
- Consumes: the Task 1 guarantee that `openid` is required/default/advertised, added to optional-only registration and authorization scopes, and the existing `OIDC_VERIFY_ID_TOKEN` environment variable.
- Produces: standalone/container operator guidance for client scopes and upgrades that distinguishes opaque access-token providers from JWT access-token providers and gives exact Authelia settings.

- [ ] **Step 1: Update the entrypoint help text for opaque-token providers**

Replace the `OIDC_VERIFY_ID_TOKEN` description in `main_oidc`'s docstring with:

```python
    - OIDC_VERIFY_ID_TOKEN (optional, default: false): Set true for providers that issue
      opaque access tokens (e.g. Authelia, or Auth0 without an API audience).
```

- [ ] **Step 2: Update the environment-variable table**

Replace the relevant rows with:

```markdown
| `OIDC_VERIFY_ID_TOKEN` | Optional. Set `true` for OIDC providers that issue opaque access tokens the default JWT verifier cannot validate (e.g. Authelia, or Auth0 without an API audience configured). ha-mcp always requests `openid`, so these providers return the ID token FastMCP verifies instead. | `false` |
| `LOG_LEVEL` | Logging level for ha-mcp and, when FastMCP logging is enabled, FastMCP's OIDC token-validation diagnostics | `INFO` |
| `FASTMCP_LOG_ENABLED` | Set `false` to disable FastMCP framework logging entirely. ha-mcp preserves this opt-out instead of routing FastMCP records through its root logger. | `true` |
```

Keep the existing `OIDC_AUDIENCE` row between these rows.

- [ ] **Step 3: State the required client scope in IdP registration**

Add this bullet after the authorization-code grant type:

```markdown
- **Allowed scope:** `openid`. ha-mcp always requests this protocol-level OIDC
  scope, so the provider must allow it for the registered client.
```

- [ ] **Step 4: Replace provider compatibility guidance with exact opaque-token behavior**

Retain the existing JWT-provider examples, then state that opaque-token
providers need `OIDC_VERIFY_ID_TOKEN=true`. Tell Authelia operators to allow
`openid`, keep its default opaque access tokens, and avoid changing
`access_token_signed_response_alg`; retain the existing Auth0 audience note.

- [ ] **Step 5: Document client scope behavior and upgrade state**

Add a client-scopes-and-upgrades section that states all of the following:

- `openid` is required, defaulted, and the only scope advertised in
  protected-resource metadata;
- explicitly requested optional scopes are retained and `openid` is added if
  the client omitted it;
- every authorization request independently unions in `openid`, while the
  upstream IdP decides whether requested optional scopes are granted;
- persisted pre-fix DCR registrations are retained and lazily migrated;
- refresh tokens without `openid` are rejected, causing one reauthorization
  while retaining the DCR registration; and
- manual client-side connector or authorization-cache clearing is only a
  fallback when a client does not restart authorization automatically.

- [ ] **Step 6: Review the rendered prose against the standalone/container boundary**

Run:

```bash
rg -n "openid|optional|authorization|DCR|refresh|cache|Authelia|OIDC_VERIFY_ID_TOKEN|access_token_signed_response_alg|LOG_LEVEL|FASTMCP_LOG_ENABLED" docs/oidc.md src/ha_mcp/__main__.py
git diff -- docs/oidc.md src/ha_mcp/__main__.py
git diff --check
```

Expected: every instruction refers to OIDC mode, no add-on/custom-component guidance changes, and the diff has no whitespace errors.

- [ ] **Step 7: Verify location and branch, then commit the documentation**

Run:

```bash
pwd
git rev-parse --abbrev-ref HEAD
git add docs/oidc.md src/ha_mcp/__main__.py
git commit -m "docs: explain Authelia OIDC configuration"
```

Expected: an atomic documentation commit on `agent/fix-oidc-authelia`.

---

### Task 4: Verify the complete change and prove the regressions

**Files:**
- Verify: `src/ha_mcp/auth/oidc_compat.py`
- Verify: `src/ha_mcp/__main__.py`
- Verify: `tests/src/unit/test_oidc_compat.py`
- Verify: `tests/src/unit/test_oidc_entrypoint.py`
- Verify: `docs/oidc.md`

**Interfaces:**
- Consumes: the completed Task 1-3 commits.
- Produces: fresh local evidence for unit behavior, formatting, lint, typing, and red-green causality before publication.

- [ ] **Step 1: Run both complete OIDC unit files**

Run:

```bash
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia/tests && HA_MCP_CONFIG_DIR=/tmp/ha-mcp-2189-final-unit UV_LINK_MODE=copy uv run pytest src/unit/test_oidc_compat.py src/unit/test_oidc_entrypoint.py -q'
```

Expected: all tests in both focused OIDC unit files pass, including optional-only
DCR and upstream-authorization scope normalization.

- [ ] **Step 2: Run formatting, lint, and type verification**

Run each command separately from the worktree:

```bash
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia && UV_LINK_MODE=copy uv run ruff format --check src/ha_mcp/auth/oidc_compat.py src/ha_mcp/__main__.py tests/src/unit/test_oidc_compat.py tests/src/unit/test_oidc_entrypoint.py'
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia && UV_LINK_MODE=copy uv run ruff check src/ha_mcp/auth/oidc_compat.py src/ha_mcp/__main__.py tests/src/unit/test_oidc_compat.py tests/src/unit/test_oidc_entrypoint.py'
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia && UV_LINK_MODE=copy uv run mypy src/'
git diff --check origin/master...HEAD
```

Expected: every command exits zero. These focused local checks cover the OIDC
change but are not complete E2E evidence.

- [ ] **Step 3: Require terminal E2E evidence from the pull request workflows**

There is no live OIDC/IdP fixture in `tests/src/e2e/`, so do not invent or run a
local OIDC E2E command. The required broad evidence comes from the pull request's
full `E2E Tests` workflow and every applicable `HAOS E2E Tests` lane, including
`HAOS E2E Tests (embedded)` and `HAOS E2E Tests (inaddon)` when scheduled.

Use only read-only GitHub inspection commands:

```bash
gh pr checks 2194 --repo homeassistant-ai/ha-mcp --watch
gh pr checks 2194 --repo homeassistant-ai/ha-mcp --json name,state,bucket,link,workflow
gh run list --repo homeassistant-ai/ha-mcp --branch agent/fix-oidc-authelia --limit 100 --json databaseId,workflowName,status,conclusion,headSha,url
gh run view <run-id> --repo homeassistant-ai/ha-mcp --log-failed
```

Expected: the full `E2E Tests` workflow and all applicable `HAOS E2E Tests`
lanes reach terminal success on the current head. If a lane fails, use its run
ID with the final command to inspect the failed log. Do not claim verification
complete while any required E2E lane is queued, in progress, failed, or missing.

- [ ] **Step 4: Temporarily remove the entrypoint and logging fixes with `apply_patch`**

Use `apply_patch` to remove only:

```python
        "required_scopes": ["openid"],
```

and the complete FastMCP settings branch:

```python
    fastmcp_logger = logging.getLogger("fastmcp")
    if fastmcp.settings.log_enabled:
        fastmcp_logger.setLevel(log_level)
    else:
        fastmcp_logger.setLevel(logging.CRITICAL + 1)
```

Retain the regression tests. Do not stage or commit this temporary mutation.
The compatibility module's import and behavior red states were already
captured in Task 1; do not replace `valid_scopes=None` with all provider scopes
for a mutation test because that would recreate the over-broad advertised-scope
behavior the design rejects.

- [ ] **Step 5: Run three regression tests and require failure**

Run:

```bash
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia/tests && HA_MCP_CONFIG_DIR=/tmp/ha-mcp-2189-mutation UV_LINK_MODE=copy uv run pytest src/unit/test_oidc_entrypoint.py::TestRunOidcServer::test_creates_oidc_proxy_with_required_openid_scope src/unit/test_oidc_entrypoint.py::TestMainOidcLogging::test_setup_logging_configures_fastmcp_logger src/unit/test_oidc_entrypoint.py::TestMainOidcLogging::test_setup_logging_preserves_fastmcp_logging_opt_out -q'
```

Expected: all three tests fail for their intended reasons: missing
`required_scopes`, enabled FastMCP remaining at INFO, and disabled FastMCP
records inheriting the root handler.

- [ ] **Step 6: Restore both fixes with `apply_patch` and rerun the regressions**

Restore the exact Task 1 scope line/comment and Task 2 logger-level line/comment. Then run:

```bash
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia/tests && HA_MCP_CONFIG_DIR=/tmp/ha-mcp-2189-restored UV_LINK_MODE=copy uv run pytest src/unit/test_oidc_entrypoint.py::TestRunOidcServer::test_creates_oidc_proxy_with_required_openid_scope src/unit/test_oidc_entrypoint.py::TestMainOidcLogging::test_setup_logging_configures_fastmcp_logger src/unit/test_oidc_entrypoint.py::TestMainOidcLogging::test_setup_logging_preserves_fastmcp_logging_opt_out -q'
git diff --check
git status --short --branch
```

Expected: `3 passed` and a clean tracked working tree. If restoration changes a committed line, compare it with `git diff HEAD -- src/ha_mcp/__main__.py` and correct it before proceeding.

- [ ] **Step 7: Obtain approval, then request the read-only code review**

Resolve the literal base and head SHAs with:

```bash
git merge-base origin/master HEAD
git rev-parse HEAD
```

Show the user the exact review scope and wait for explicit approval before
summoning a Codex reviewer. After approval, use the
`superpowers:requesting-code-review` reviewer template. Give the reviewer the
approved spec at
`docs/superpowers/specs/2026-08-10-oidc-authelia-scope-design.md`, this plan,
the literal SHAs returned above, and a read-only instruction. Require review
of plan alignment, OIDC security/compatibility, logger behavior, tests, and
operator documentation.

Expected: no Critical or Important finding remains. Validate every finding against current source and policy; fix valid findings with a new TDD cycle and atomic commit, rerun Task 4 verification, then request a fresh review.

---

### Task 5: Publish a draft PR and run the resolution and approval loop

**Files:**
- Publish: commits on `agent/fix-oidc-authelia`
- Create: one pull request against `homeassistant-ai/ha-mcp:master` that begins
  as a draft.

**Interfaces:**
- Consumes: clean reviewed commits and verified GitHub identity.
- Produces: a PR containing `Fixes #2189`, green CI, and no unresolved review
  threads. It begins as a draft and is marked ready only after explicit user
  approval.

- [ ] **Step 1: Verify final scope, identity, remotes, and branch**

Run:

```bash
pwd
git rev-parse --abbrev-ref HEAD
git status --short --branch
git diff --stat origin/master...HEAD
git log --oneline origin/master..HEAD
git remote -v
gh auth status
```

Expected: the worktree is clean, branch is `agent/fix-oidc-authelia`, only the design/plan/scope/logging/docs commits are present, and the authenticated GitHub identity owns the selected writable fork remote.

- [ ] **Step 2: Push the feature branch**

Push to the verified writable fork remote. In the current checkout that remote is expected to be `fork`:

```bash
git push -u fork agent/fix-oidc-authelia
```

Expected: the branch is published without modifying `origin/master`.

- [ ] **Step 3: Create the required draft PR with the exact scope**

Create a temporary PR body with `apply_patch` at `/tmp/ha-mcp-2189-pr-body.md` containing:

```markdown
## Summary

- require `openid` while retaining explicitly requested optional OIDC scopes
- preserve DCR registrations across upgrade and require one reauthorization for legacy refresh tokens
- make `LOG_LEVEL` control FastMCP's OIDC token-validation diagnostics
- document Authelia's default opaque access-token configuration

## Testing

- `cd tests && UV_LINK_MODE=copy uv run pytest src/unit/test_oidc_compat.py src/unit/test_oidc_entrypoint.py -q`
- `UV_LINK_MODE=copy uv run ruff format --check src/ha_mcp/auth/oidc_compat.py src/ha_mcp/__main__.py tests/src/unit/test_oidc_compat.py tests/src/unit/test_oidc_entrypoint.py`
- `UV_LINK_MODE=copy uv run ruff check src/ha_mcp/auth/oidc_compat.py src/ha_mcp/__main__.py tests/src/unit/test_oidc_compat.py tests/src/unit/test_oidc_entrypoint.py`
- `UV_LINK_MODE=copy uv run mypy src/`

Fixes #2189
```

Then run:

```bash
gh pr create --draft --repo homeassistant-ai/ha-mcp --base master --head kingpanther13:agent/fix-oidc-authelia --title "fix: support opaque access tokens in OIDC mode" --body-file /tmp/ha-mcp-2189-pr-body.md
```

Expected: one draft PR URL. Read it back with `gh pr view --repo homeassistant-ai/ha-mcp --json number,url,isDraft,author,title,body,headRefName,baseRefName` and verify `isDraft: true`, the expected authenticated author, exact head/base, title, body, and `Fixes #2189`.

- [ ] **Step 4: Wait for and inspect all CI checks**

Use the branch name so the command resolves the newly created PR directly:

```bash
gh pr checks agent/fix-oidc-authelia --repo homeassistant-ai/ha-mcp --watch
```

Expected: every required check reaches a terminal success state. If a deterministic workflow fails, resolve its run ID and inspect it once:

```bash
TASK_FAILED_RUN_ID=$(gh run list --repo homeassistant-ai/ha-mcp --branch agent/fix-oidc-authelia --status failure --limit 1 --json databaseId --jq '.[0].databaseId')
gh run view "$TASK_FAILED_RUN_ID" --repo homeassistant-ai/ha-mcp --log-failed
```

Diagnose and fix the root cause rather than repeatedly rerunning it.

- [ ] **Step 5: Inspect comments and thread state after CI**

Resolve the PR number from the unique head branch, then run all reads in the
same shell session:

```bash
TASK_PR_NUMBER=$(gh pr view agent/fix-oidc-authelia --repo homeassistant-ai/ha-mcp --json number --jq '.number')
gh api "repos/homeassistant-ai/ha-mcp/issues/$TASK_PR_NUMBER/comments"
gh api "repos/homeassistant-ai/ha-mcp/pulls/$TASK_PR_NUMBER/comments"
gh api graphql -F pr="$TASK_PR_NUMBER" -f query='query($pr: Int!) { repository(owner:"homeassistant-ai", name:"ha-mcp") { pullRequest(number:$pr) { reviews(first:100) { nodes { author { login } state body } } reviewThreads(first:100) { nodes { id isResolved comments(first:100) { nodes { databaseId author { login } body path line } } } } } } }'
```

Expected: all human and automated feedback is accounted for and no actionable unresolved thread remains.

- [ ] **Step 6: Resolve failures or feedback for at most five iterations**

For each valid finding, reproduce it where practical, add or update a regression test before production code, make an atomic commit after verifying branch/worktree location, push, and return to Steps 4-5. Reply to inline comments with the fixing commit and resolve their GraphQL thread only after the fix is pushed and verified. Do not dismiss technically questionable feedback without checking current source, docs, and tests.

Expected terminal state before readiness approval: all CI green, all inline
review threads addressed, no unresolved actionable feedback, and the PR still
draft. If the same external blocker survives five resolution iterations,
report the exact blocker rather than marking the PR ready or merging it.

- [ ] **Step 7: Obtain explicit approval, then mark the PR ready**

Present the final CI and inline-thread state and wait for explicit user
approval. Only after that approval, run:

```bash
gh pr ready agent/fix-oidc-authelia --repo homeassistant-ai/ha-mcp
```

Expected: the PR transitions from draft to ready. Do not post a top-level PR
comment; only reply within existing inline review threads when addressing
their feedback.

- [ ] **Step 8: Verify the final PR state and report**

Run:

```bash
gh pr view agent/fix-oidc-authelia --repo homeassistant-ai/ha-mcp --json number,url,isDraft,mergeStateStatus,reviewDecision,statusCheckRollup,comments,reviews
git status --short --branch
```

Expected: after approval, `isDraft` is false and the final report can cite the
PR URL, exact focused local verification, terminal CI status, and review-thread
status. Do not merge, close the issue manually, delete the branch, or remove
the worktree.
