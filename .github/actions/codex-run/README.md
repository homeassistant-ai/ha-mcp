# Generic Codex action

`codex-run` passes the caller's `instructions`, or the contents of its
`instructions-file`, to `codex exec` verbatim. It does not add a role, output
contract, repository context, or task wrapper.
Hosted ChatGPT apps/connectors are disabled so they cannot bypass the GitHub
permissions selected by the workflow caller. Callers may collect authorized
context into workspace files or expose narrowly scoped command-line tokens.

The caller owns:

- GitHub `permissions` and whether `GH_TOKEN` is exported;
- checkout strategy and credentials;
- read-only versus workspace-write sandboxing;
- whether the agent receives a shell tool;
- instructions, expected output, validation and any later side effects;
- concurrency for workflows that share one `CODEX_AUTH` refresh token.

`CODEX_AUTH` contains the raw `auth.json` JSON, not Base64. The action writes it
to an isolated `CODEX_HOME` under `RUNNER_TEMP`, pins an exact Codex CLI
version, runs ephemerally, and exposes the final-message and auth paths.
Each invocation receives unique paths, so a job can call the action more than
once without overwriting an earlier result or auth snapshot. The invocation
timeout should remain shorter than the caller's job timeout, leaving time for
the separate auth-persistence step.

When the caller also supplies a repository-scoped token with `Secrets: write`,
it can invoke the separate `codex-update-auth` action under `if: always()` to
persist a refreshed `auth.json`. Keeping this step separate prevents ordinary
read or write permissions from implicitly granting secret administration.

## Read-only example

```yaml
permissions:
  contents: read

jobs:
  analyze:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          persist-credentials: false
      - uses: ./.github/actions/codex-run
        with:
          codex-auth: ${{ secrets.CODEX_AUTH }}
          sandbox: read-only
          instructions: |
            Inspect this repository and report the three highest-risk test gaps.
            Do not modify files or external state.
```

## Caller-controlled GitHub access

```yaml
permissions:
  contents: read
  issues: read

jobs:
  analyze-issues:
    runs-on: ubuntu-latest
    env:
      GH_TOKEN: ${{ github.token }}
    steps:
      - uses: actions/checkout@v7
        with:
          persist-credentials: false
      - uses: ./.github/actions/codex-run
        with:
          codex-auth: ${{ secrets.CODEX_AUTH }}
          sandbox: read-only
          allow-shell: "false"
          instructions-file: .codex-context/issue-review-prompt.txt
```

For repository edits, the caller may choose `sandbox: workspace-write` and
grant only the GitHub permissions needed by later workflow steps. The action
never commits, pushes, creates issues, comments, or opens pull requests itself.

Only use Codex authentication with trusted workflow events and trusted
instructions. Do not expose `CODEX_AUTH` to workflows that execute untrusted
fork code. Workflows sharing one auth secret should use the same non-cancelling
`concurrency` group with `queue: max` so two jobs cannot refresh the same token
simultaneously and queued manual reports are not evicted.

Issue bodies, PR descriptions, patches and review comments are attacker-
controlled data. A read-only filesystem sandbox still permits reads, including
of `auth.json`. A caller processing such data must set `allow-shell: "false"`
and place the complete prompt plus context in an `instructions-file`. Disabling
the shell leaves the model with no local file-reading tool, so the caller must
embed every required artifact directly in that file.

## Validated examples

Validated on 2026-08-31 in the dedicated
[`ha-mcp-workflows-dev`](https://github.com/homeassistant-ai/ha-mcp-workflows-dev)
bench with Codex CLI `0.151.0`:

- [Hello World run 33459225512](https://github.com/homeassistant-ai/ha-mcp-workflows-dev/actions/runs/33459225512)
  passed the verbatim instruction assertion and forced a `CODEX_AUTH` rewrite
  through the renewed repository-scoped secret-writer PAT.
- [Issue review run 33459227873](https://github.com/homeassistant-ai/ha-mcp-workflows-dev/actions/runs/33459227873)
  analyzed fixtures `#62`–`#65`, including the deliberate `#62/#63` duplicate,
  from a caller-built prompt with the shell disabled.
- [Pull-request review run 33459231108](https://github.com/homeassistant-ai/ha-mcp-workflows-dev/actions/runs/33459231108)
  analyzed fixtures `#66`–`#68` from caller-collected metadata, patches and
  inline review-thread state with the shell disabled.

The review workflows write only to the Actions log and step summary. Hosted
ChatGPT connectors remain disabled, so repository visibility is bounded by the
context that the caller collects with its declared GitHub permissions.
