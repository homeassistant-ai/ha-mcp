# OIDC Authelia Scope and Diagnostics Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the standalone/container `ha-mcp-oidc` entrypoint work with Authelia's default opaque access tokens by always requesting `openid`, while making `LOG_LEVEL` control FastMCP's OIDC diagnostics and documenting the required configuration.

**Architecture:** Keep FastMCP's existing OIDC proxy and token-verification design. Supply the protocol-level `openid` scope through `OIDCProxy.required_scopes`, retain opt-in ID-token verification for opaque access-token providers, and bridge ha-mcp's common logging level into FastMCP's non-propagating logger without replacing FastMCP's handlers.

**Tech Stack:** Python 3.13, FastMCP 3.4.6 `OIDCProxy`, MCP Python SDK 1.28.1, pytest/pytest-asyncio, Ruff, mypy, Markdown, GitHub CLI.

## Global Constraints

- Work only in `/data/data/com.termux/files/home/ha-mcp/worktree/fix-oidc-authelia` on branch `agent/fix-oidc-authelia`; never modify or commit from `master` or the user's dirty main checkout.
- The behavior change applies only to the standalone/container `ha-mcp-oidc` entrypoint; do not alter the Home Assistant add-on, custom component, OAuth mode, audience/resource forwarding, redirect URI policy, or token endpoint authentication.
- Always pass exactly `required_scopes=["openid"]`; do not make OIDC scopes configurable because `openid` is mandatory for this OIDC entrypoint.
- Keep `OIDC_VERIFY_ID_TOKEN` opt-in: JWT access-token providers retain default verification, while Authelia and other opaque-access-token providers set it to `true`.
- Preserve FastMCP's handlers and formatting; only set the `fastmcp` logger's level to the validated ha-mcp `LOG_LEVEL` value.
- Do not require Authelia JWT access tokens or add introspection support.
- Follow red-green TDD for both behavior changes, use `apply_patch` for source edits, and verify the branch and worktree before every commit.
- Run Python setup and verification in the existing Ubuntu `proot-distro` environment with `UV_LINK_MODE=copy`; run pytest from the repository's `tests/` directory.
- Create the GitHub pull request as a draft and do not mark it ready for review without explicit user approval.

## File Map

- `tests/src/unit/test_oidc_entrypoint.py`: pinned `OIDCProxy` constructor contract plus regression coverage for the required OIDC scope and FastMCP logger level.
- `src/ha_mcp/__main__.py`: common logging setup and standalone/container OIDC proxy construction.
- `docs/oidc.md`: operator guidance for `openid`, Authelia's opaque tokens, ID-token verification, and debug logging.
- `docs/superpowers/specs/2026-08-10-oidc-authelia-scope-design.md`: already-approved behavior and non-goals; no further edit is planned.

---

### Task 1: Require the `openid` scope in OIDC mode

**Files:**
- Modify: `tests/src/unit/test_oidc_entrypoint.py:18-58,270-325,1177-1209`
- Modify: `src/ha_mcp/__main__.py:1738-1754`

**Interfaces:**
- Consumes: FastMCP 3.4.6 `OIDCProxy.__init__(..., required_scopes: list[str] | None = None, ...)`.
- Produces: `_run_oidc_server(...) -> None` constructs every OIDC proxy with `required_scopes=["openid"]`; the pinned mock records that exact list.

- [ ] **Step 1: Verify the isolated branch and install the development environment**

Run from the worktree:

```bash
pwd
git rev-parse --abbrev-ref HEAD
git status --short --branch
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia && UV_LINK_MODE=copy uv sync --group dev'
```

Expected: the path ends in `worktree/fix-oidc-authelia`, the branch is `agent/fix-oidc-authelia`, status contains only the committed design plus this plan when appropriate, and `uv sync` completes successfully.

- [ ] **Step 2: Run the untouched OIDC unit file as a baseline**

Run:

```bash
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia/tests && HA_MCP_CONFIG_DIR=/tmp/ha-mcp-2189-baseline UV_LINK_MODE=copy uv run pytest src/unit/test_oidc_entrypoint.py -q'
```

Expected: PASS. If it does not pass before test edits, stop and distinguish an environment/baseline failure from issue #2189 before changing production code.

- [ ] **Step 3: Extend the pinned proxy mock and real-signature contract**

In `_make_mock_oidc_proxy`, document `required_scopes` as a mandatory production argument, insert it after `require_authorization_consent`, and record it unconditionally:

```python
    ``required_scopes`` is mandatory in production. The remaining optional
    constructor arguments default to the ``_UNSET`` sentinel and are only
    recorded in ``capture`` when actually passed.

    class MockOIDCProxy:
        def __init__(
            self,
            *,
            config_url,
            client_id,
            client_secret,
            base_url,
            require_authorization_consent,
            required_scopes,
            jwt_signing_key,
            allowed_client_redirect_uris=_UNSET,
            verify_id_token=_UNSET,
            audience=_UNSET,
        ):
            capture["config_url"] = config_url
            capture["client_id"] = client_id
            capture["client_secret"] = client_secret
            capture["base_url"] = base_url
            capture["require_authorization_consent"] = require_authorization_consent
            capture["required_scopes"] = required_scopes
            capture["jwt_signing_key"] = jwt_signing_key
```

Add `"required_scopes"` to `run_oidc_server_kwargs` in `TestOIDCProxySignatureSubset`:

```python
        run_oidc_server_kwargs = {
            "config_url",
            "client_id",
            "client_secret",
            "base_url",
            "require_authorization_consent",
            "required_scopes",
            "jwt_signing_key",
            "allowed_client_redirect_uris",
            "verify_id_token",
            "audience",
        }
```

- [ ] **Step 4: Write the failing OIDC-scope regression assertion**

Rename the existing broad construction test and extend its contract:

```python
    @pytest.mark.asyncio
    async def test_creates_oidc_proxy_with_required_openid_scope(self):
        """OIDC mode should always require the protocol-level openid scope."""
```

Keep the existing setup and assertions, then add:

```python
        assert proxy_init_args["required_scopes"] == ["openid"]
```

- [ ] **Step 5: Run the regression test and verify the red state**

Run:

```bash
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia/tests && HA_MCP_CONFIG_DIR=/tmp/ha-mcp-2189-red-scope UV_LINK_MODE=copy uv run pytest src/unit/test_oidc_entrypoint.py::TestRunOidcServer::test_creates_oidc_proxy_with_required_openid_scope -q'
```

Expected: FAIL with `TypeError` reporting that `MockOIDCProxy.__init__()` is missing the required keyword-only argument `required_scopes`. This proves current production never supplies the scope.

- [ ] **Step 6: Implement the minimum production fix**

Add the required scope to the always-present `proxy_kwargs` mapping immediately after the consent setting:

```python
        # This entrypoint provides OIDC, so request the protocol-level scope
        # that makes upstream providers return an ID token. FastMCP also
        # advertises this scope through protected resource metadata so MCP
        # clients include it in the authorization flow.
        "required_scopes": ["openid"],
```

The complete surrounding contract must remain:

```python
    proxy_kwargs: dict[str, Any] = {
        "config_url": config_url,
        "client_id": client_id,
        "client_secret": client_secret,
        "base_url": base_url,
        # "external" tells FastMCP that consent is handled by the upstream
        # IdP (Authentik, Keycloak, etc.) -- unlike `False`, this does not
        # log a security warning at startup that consent is disabled.
        "require_authorization_consent": "external",
        # This entrypoint provides OIDC, so request the protocol-level scope
        # that makes upstream providers return an ID token. FastMCP also
        # advertises this scope through protected resource metadata so MCP
        # clients include it in the authorization flow.
        "required_scopes": ["openid"],
        # Preserve `or None`: an empty-but-set env var must not bypass
        # FastMCP's derive-from-client-secret default for jwt_signing_key.
        "jwt_signing_key": os.getenv("OIDC_JWT_SIGNING_KEY") or None,
    }
```

- [ ] **Step 7: Run the scope and signature tests and verify green**

Run:

```bash
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia/tests && HA_MCP_CONFIG_DIR=/tmp/ha-mcp-2189-green-scope UV_LINK_MODE=copy uv run pytest src/unit/test_oidc_entrypoint.py::TestRunOidcServer::test_creates_oidc_proxy_with_required_openid_scope src/unit/test_oidc_entrypoint.py::TestOIDCProxySignatureSubset::test_run_oidc_server_kwargs_are_subset_of_oidc_proxy_params -q'
```

Expected: `2 passed`.

- [ ] **Step 8: Verify location and branch, then commit the scope regression and fix**

Run:

```bash
pwd
git rev-parse --abbrev-ref HEAD
git diff --check
git add src/ha_mcp/__main__.py tests/src/unit/test_oidc_entrypoint.py
git commit -m "fix: require openid scope in OIDC mode"
```

Expected: a commit on `agent/fix-oidc-authelia`, never on `master` or `main`.

---

### Task 2: Make `LOG_LEVEL` control FastMCP diagnostics

**Files:**
- Modify: `tests/src/unit/test_oidc_entrypoint.py:7-10,187-269`
- Modify: `src/ha_mcp/__main__.py:485-518`

**Interfaces:**
- Consumes: `_setup_logging(log_level_str: str, force: bool = True) -> None` and Python's `logging.getLogger("fastmcp")`.
- Produces: `_setup_logging` sets both the root configuration and the `fastmcp` logger to the same numeric level while leaving FastMCP's handler and `propagate` configuration untouched.

- [ ] **Step 1: Write the failing FastMCP logging regression test**

Add `import logging` with the standard-library imports at the top of `tests/src/unit/test_oidc_entrypoint.py`:

```python
import logging
import os
```

Add this test to `TestMainOidcLogging`:

```python
    def test_setup_logging_configures_fastmcp_logger(self):
        """LOG_LEVEL should apply to FastMCP's non-propagating logger."""
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

- [ ] **Step 3: Implement the minimum common logging bridge**

Resolve the numeric level once and reuse it in `_setup_logging`:

```python
    log_level = getattr(logging, log_level_str)

    from ha_mcp.utils.usage_logger import preserve_startup_collector

    with preserve_startup_collector():
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s %(name)s %(levelname)s: %(message)s",
            datefmt=_LOG_DATE_FORMAT,
            force=force,
        )

    # FastMCP configures its own handler and disables propagation, so the root
    # level above does not control its OIDC diagnostics. Preserve that handler
    # and formatting while honoring ha-mcp's LOG_LEVEL setting.
    logging.getLogger("fastmcp").setLevel(log_level)
```

Leave the existing `mcp.server.streamable_http` and `fastmcp.server.server` filters immediately after this block.

- [ ] **Step 4: Run the logging regression test and verify green**

Run:

```bash
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia/tests && HA_MCP_CONFIG_DIR=/tmp/ha-mcp-2189-green-logging UV_LINK_MODE=copy uv run pytest src/unit/test_oidc_entrypoint.py::TestMainOidcLogging::test_setup_logging_configures_fastmcp_logger -q'
```

Expected: `1 passed`.

- [ ] **Step 5: Run all logging and OIDC proxy construction tests**

Run:

```bash
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia/tests && HA_MCP_CONFIG_DIR=/tmp/ha-mcp-2189-green-focused UV_LINK_MODE=copy uv run pytest src/unit/test_oidc_entrypoint.py::TestMainOidcLogging src/unit/test_oidc_entrypoint.py::TestRunOidcServer src/unit/test_oidc_entrypoint.py::TestOIDCProxySignatureSubset -q'
```

Expected: all selected tests pass.

- [ ] **Step 6: Verify location and branch, then commit the logging regression and fix**

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
- Consumes: the Task 1 guarantee that `openid` is always requested and the existing `OIDC_VERIFY_ID_TOKEN` environment variable.
- Produces: standalone/container operator guidance that distinguishes opaque access-token providers from JWT access-token providers and gives exact Authelia settings.

- [ ] **Step 1: Update the entrypoint help text for opaque-token providers**

Replace the `OIDC_VERIFY_ID_TOKEN` description in `main_oidc`'s docstring with:

```python
    - OIDC_VERIFY_ID_TOKEN (optional, default: false): Set true for providers that issue
      opaque access tokens (e.g. Authelia, or Auth0 without an API audience).
```

- [ ] **Step 2: Update the environment-variable table**

Replace the two relevant rows with:

```markdown
| `OIDC_VERIFY_ID_TOKEN` | Optional. Set `true` for OIDC providers that issue opaque access tokens the default JWT verifier cannot validate (e.g. Authelia, or Auth0 without an API audience configured). ha-mcp always requests `openid`, so these providers return the ID token FastMCP verifies instead. | `false` |
| `LOG_LEVEL` | Logging level for ha-mcp and FastMCP, including OIDC token-validation diagnostics | `INFO` |
```

Keep the existing `OIDC_AUDIENCE` row between these rows.

- [ ] **Step 3: State the required client scope in IdP registration**

Add this bullet after the authorization-code grant type:

```markdown
- **Allowed scope:** `openid`. ha-mcp always requests this protocol-level OIDC
  scope, so the provider must allow it for the registered client.
```

- [ ] **Step 4: Replace provider compatibility guidance with exact opaque-token behavior**

Replace the current `Provider Compatibility` body with:

```markdown
OIDC mode works out of the box with providers that issue **JWT access
tokens** — Authentik and Keycloak are known to work without extra
configuration.

ha-mcp always requests the required `openid` scope. Providers that issue
**opaque access tokens** (not JWTs) also need `OIDC_VERIFY_ID_TOKEN=true` so
FastMCP verifies the returned ID token instead of trying to parse the access
token as a JWT:

- **Authelia** issues opaque access tokens by default. Allow `openid` in the
  client's `scopes` and set `OIDC_VERIFY_ID_TOKEN=true` for ha-mcp. You do not
  need to change Authelia's `access_token_signed_response_alg` to enable JWT
  access tokens.
- **Auth0** issues opaque access tokens unless the client requests a
  configured API audience.
```

- [ ] **Step 5: Review the rendered prose against the standalone/container boundary**

Run:

```bash
rg -n "openid|Authelia|OIDC_VERIFY_ID_TOKEN|access_token_signed_response_alg|LOG_LEVEL" docs/oidc.md src/ha_mcp/__main__.py
git diff -- docs/oidc.md src/ha_mcp/__main__.py
git diff --check
```

Expected: every instruction refers to OIDC mode, no add-on/custom-component guidance changes, and the diff has no whitespace errors.

- [ ] **Step 6: Verify location and branch, then commit the documentation**

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
- Verify: `src/ha_mcp/__main__.py`
- Verify: `tests/src/unit/test_oidc_entrypoint.py`
- Verify: `docs/oidc.md`

**Interfaces:**
- Consumes: the completed Task 1-3 commits.
- Produces: fresh local evidence for unit behavior, formatting, lint, typing, and red-green causality before publication.

- [ ] **Step 1: Run the complete OIDC unit file**

Run:

```bash
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia/tests && HA_MCP_CONFIG_DIR=/tmp/ha-mcp-2189-final-unit UV_LINK_MODE=copy uv run pytest src/unit/test_oidc_entrypoint.py -q'
```

Expected: all tests in `test_oidc_entrypoint.py` pass.

- [ ] **Step 2: Run formatting, lint, and type verification**

Run each command separately from the worktree:

```bash
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia && UV_LINK_MODE=copy uv run ruff format --check src/ha_mcp/__main__.py tests/src/unit/test_oidc_entrypoint.py'
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia && UV_LINK_MODE=copy uv run ruff check src/ha_mcp/__main__.py tests/src/unit/test_oidc_entrypoint.py'
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia && UV_LINK_MODE=copy uv run mypy src/'
git diff --check origin/master...HEAD
```

Expected: every command exits zero. Do not describe repository-wide tests as locally passing because no relevant live-IdP/E2E fixture exists; GitHub's ordinary E2E workflow supplies the repository-wide lane.

- [ ] **Step 3: Run the repository's full E2E command**

The repository's issue-to-PR workflow requires the full E2E command even
though this entrypoint has no live-IdP E2E fixture. Run it and let pytest
report the actual Docker availability rather than assuming prerequisites are
missing:

```bash
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia/tests && UV_LINK_MODE=copy uv run pytest src/e2e/ -n2 --dist loadscope -v --tb=short'
```

Expected: the suite passes when Docker is available. If the Android/proot
environment cannot reach a Docker daemon, retain the exact collection or
connection failure as local-environment evidence and require the GitHub E2E
workflow to pass before the PR is reported merge-ready.

- [ ] **Step 4: Temporarily remove both production fixes with `apply_patch`**

Use `apply_patch` to remove only:

```python
        "required_scopes": ["openid"],
```

and:

```python
    logging.getLogger("fastmcp").setLevel(log_level)
```

Retain the regression tests. Do not stage or commit this temporary mutation.

- [ ] **Step 5: Run both regression tests and require failure**

Run:

```bash
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia/tests && HA_MCP_CONFIG_DIR=/tmp/ha-mcp-2189-mutation UV_LINK_MODE=copy uv run pytest src/unit/test_oidc_entrypoint.py::TestRunOidcServer::test_creates_oidc_proxy_with_required_openid_scope src/unit/test_oidc_entrypoint.py::TestMainOidcLogging::test_setup_logging_configures_fastmcp_logger -q'
```

Expected: both tests fail for their intended reasons: missing `required_scopes` and FastMCP remaining at INFO.

- [ ] **Step 6: Restore both fixes with `apply_patch` and rerun the regressions**

Restore the exact Task 1 scope line/comment and Task 2 logger-level line/comment. Then run:

```bash
proot-distro login ubuntu --bind /data/data/com.termux/files/home:/mnt/termux -- sh -lc 'cd /mnt/termux/ha-mcp/worktree/fix-oidc-authelia/tests && HA_MCP_CONFIG_DIR=/tmp/ha-mcp-2189-restored UV_LINK_MODE=copy uv run pytest src/unit/test_oidc_entrypoint.py::TestRunOidcServer::test_creates_oidc_proxy_with_required_openid_scope src/unit/test_oidc_entrypoint.py::TestMainOidcLogging::test_setup_logging_configures_fastmcp_logger -q'
git diff --check
git status --short --branch
```

Expected: `2 passed` and a clean tracked working tree. If restoration changes a committed line, compare it with `git diff HEAD -- src/ha_mcp/__main__.py` and correct it before proceeding.

- [ ] **Step 7: Request the required read-only code review**

Resolve the literal base and head SHAs with:

```bash
git merge-base origin/master HEAD
git rev-parse HEAD
```

Use the `superpowers:requesting-code-review` reviewer template. Give the reviewer the approved spec at `docs/superpowers/specs/2026-08-10-oidc-authelia-scope-design.md`, this plan, the literal SHAs returned above, and a read-only instruction. Require review of plan alignment, OIDC security/compatibility, logger behavior, tests, and operator documentation.

Expected: no Critical or Important finding remains. Validate every finding against current source and policy; fix valid findings with a new TDD cycle and atomic commit, rerun Task 4 verification, then request a fresh review.

---

### Task 5: Publish a draft PR and run the repository resolution loop

**Files:**
- Publish: commits on `agent/fix-oidc-authelia`
- Create: one draft pull request against `homeassistant-ai/ha-mcp:master`

**Interfaces:**
- Consumes: clean reviewed commits and verified GitHub identity.
- Produces: a draft PR containing `Fixes #2189`, green CI, and no unresolved review threads; it remains draft.

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

- require the `openid` scope in the standalone/container OIDC authorization flow
- make `LOG_LEVEL` control FastMCP's OIDC token-validation diagnostics
- document Authelia's default opaque access-token configuration

## Testing

- `cd tests && UV_LINK_MODE=copy uv run pytest src/unit/test_oidc_entrypoint.py -q`
- `UV_LINK_MODE=copy uv run ruff format --check src/ha_mcp/__main__.py tests/src/unit/test_oidc_entrypoint.py`
- `UV_LINK_MODE=copy uv run ruff check src/ha_mcp/__main__.py tests/src/unit/test_oidc_entrypoint.py`
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

Expected terminal state: all CI green, all comments addressed, no unresolved actionable review threads, and the PR remains draft. If the same external blocker survives five resolution iterations, report the exact blocker rather than marking the PR ready or merging it.

- [ ] **Step 7: Post the repository-required implementation summary**

After checks and threads are clean, resolve the PR number and post this exact
summary, adjusting only the local-E2E sentence if Docker was available:

```bash
TASK_PR_NUMBER=$(gh pr view agent/fix-oidc-authelia --repo homeassistant-ai/ha-mcp --json number --jq '.number')
gh pr comment "$TASK_PR_NUMBER" --repo homeassistant-ai/ha-mcp --body '## Implementation Summary

**Choices Made:**
- Always request the protocol-level `openid` scope from the standalone/container OIDC proxy.
- Keep ID-token verification opt-in for opaque access-token providers such as Authelia.
- Apply ha-mcp `LOG_LEVEL` to FastMCP without replacing FastMCP handlers.

**Problems Encountered:**
- The original flow requested no `openid` scope, so Authelia returned no ID token while its default access token remained opaque.
- The local Android/proot environment could not provide a Docker daemon; the pull request E2E workflow supplied full-suite verification.'
```

Read the comment back and verify the author and body. If Docker was available
and the full suite passed locally, replace only the second Problems
Encountered bullet with `- No implementation blockers; the full E2E suite passed locally and in GitHub Actions.`

- [ ] **Step 8: Verify the final draft PR state and report**

Run:

```bash
gh pr view agent/fix-oidc-authelia --repo homeassistant-ai/ha-mcp --json number,url,isDraft,mergeStateStatus,reviewDecision,statusCheckRollup,comments,reviews
git status --short --branch
```

Expected: the final report can cite the PR URL, exact local verification, terminal CI status, and review-thread status. Do not run `gh pr ready`, merge, close the issue manually, delete the branch, or remove the worktree.
