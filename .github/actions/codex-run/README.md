# Generic Codex action

`codex-run` executes caller instructions verbatim on Ubuntu with Codex CLI
`0.153.4`. The caller owns the task, capabilities, expected output and publication.
The action owns its OAuth credential isolation, process lifetime and diagnostics.
It does not interpret repository roles or implement a maintainer trust list.

## Invocation and capabilities

Supply exactly one of `instructions` or `instructions-file`, plus `codex-auth`
(the raw `auth.json` stored in `CODEX_AUTH`). Paths may be workspace-relative or
absolute, but `working-directory`, `instructions-file` and `output-schema` must
resolve inside `GITHUB_WORKSPACE`.

| Input | Default | Meaning |
|---|---|---|
| `sandbox` | `read-only` | `read-only` or `workspace-write`; credential paths stay denied in both modes. |
| `allow-shell` | `true` | Whether the model can execute commands. Use `false` for pre-collected untrusted reports. |
| `network-access` | `false` | Outbound network for model commands; `true` allows direct network access without a domain allowlist. It does not govern the Codex client's connection to OpenAI. |
| `passthrough-env` | empty | Exact environment variable names, one per line, explicitly granted to commands. No wildcards; unset names and the action's reserved `CODEX*` names are rejected. |
| `timeout-minutes` | `10` | Codex process limit, starting after setup. The caller must also cap the whole action step. |
| `model` / `reasoning-effort` | empty | Optional explicit model and reasoning settings. |
| `output-schema` | empty | Optional final-response JSON Schema; response usefulness and presence remain caller requirements. |
| `codex-version` | `0.153.4` | Exact CLI version, required for reproducible permission behavior. |

Commands inherit the small `core` environment. Explicit grants are added through
`shell_environment_policy.set`, so a selected `GH_TOKEN` is actually available
rather than discarded by the core environment filter. Values are JSON/TOML
quoted into a private profile under the denied credential directory; they are
never passed as command-line arguments or printed by the preparation step.
Grant only credentials/capabilities the task may use. A granted token is visible
to the model's commands and can be printed by them; its permissions are the
caller's responsibility. The action's own OAuth material must never be granted.

## Using gh

The workflow supplies both the token and the network capability. This example
permits reading GitHub repository metadata. A maintenance caller may choose the
specific write permissions its operation needs, while keeping the secret-writer
PAT outside the agent step.

```yaml
permissions:
  contents: read

jobs:
  inspect:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7
        timeout-minutes: 2
        with:
          persist-credentials: false
      - id: codex
        uses: ./.github/actions/codex-run
        timeout-minutes: 18
        env:
          GH_TOKEN: ${{ github.token }}
        with:
          codex-auth: ${{ secrets.CODEX_AUTH }}
          model: gpt-6-astra
          sandbox: read-only
          allow-shell: "true"
          network-access: "true"
          passthrough-env: GH_TOKEN
          instructions: Run gh api repos/${{ github.repository }} --jq .full_name and report its stdout.
      - uses: ./.github/actions/codex-update-auth
        timeout-minutes: 3
        if: ${{ always() && steps.codex.outputs.auth-path != '' }}
        with:
          codex-auth-path: ${{ steps.codex.outputs.auth-path }}
          original-codex-auth: ${{ steps.codex.outputs.original-auth-path }}
          gh-token: ${{ secrets.CODEX_AUTH_PAT }}
          repository: ${{ github.repository }}
```

Every workflow sharing this OAuth session must also use the same concurrency
policy as the checked-in examples: `codex-auth-${{ github.repository }}` with
`cancel-in-progress: false` and `queue: max`. The example's GitHub permissions
bound its token; enabling network does not expand them. Authority to dispatch
or interpret a maintainer command belongs in the caller, outside this action.

## Outputs, diagnostics and persistence

- `output-path`: final response file; may be empty even when the CLI succeeds.
- `log-path`: captured JSON events and CLI diagnostics, under the denied
  credential directory. The caller can deliberately publish this file for
  debugging; it may contain prompts, tool output and other sensitive context.
- `auth-path` / `original-auth-path`: paths for the separate persistence helper.
- `codex-version`: installed CLI version.

The action does not print the response or CLI transcript and does not write a
step summary. This also captures the CLI's native stdout/stderr; removing a
second copy alone would not prevent publication. Callers decide what to display
or store. The example report workflows require a nonempty response and publish
it themselves. They remain shell-less, read-only and without network grants.
A missing/invalid response is their failure, not a universal contract imposed on
arbitrary Codex tasks. Refusal text is not necessarily an empty response.

Nonzero CLI statuses are propagated with an annotation. Status 124 indicates the
process time limit; 137 indicates SIGKILL, which can have other causes as well.
Detailed diagnostics remain in `log-path`. Do not automatically publish a private
transcript on failure without considering what the caller supplied to the model.

The supplied issue/PR reports and fixed Hello World smoke deliberately print
`log-path` on failure. Workflow-command interpretation is suspended with a fresh
resume token while printing, then restored on exit; a prefix alone would not
neutralize legacy commands. Lines are prefixed for readability. Their context is public and
they grant no extra environment variables. This preserves diagnostics in the
Actions run log before the temporary runner disappears. Callers using private
context or other capabilities must choose their own diagnostic publication policy.
The report callers use the same command suspension when displaying a successful
model response; writing Markdown to the summary file does not execute commands.

Report jobs bound checkout, collection, the complete action, report publication,
failure diagnostics and auth persistence to 2/5/18/1/1/3 minutes within a
32-minute job. `always()`
cannot survive a job-level timeout, so cleanup needs its own remaining budget.

`codex-update-auth` validates the file, skips unchanged auth without needing a
working CLI, and validates login before an actual secret update. `force-update`
forces that validation/update even when the bytes are identical. Repository,
secret name and secret-writer token belong to this separate trusted step.

## Credential isolation

Each call uses a distinct `CODEX_HOME` and auth snapshot under a shared denied
root in `RUNNER_TEMP`. Both readable auth paths would cause the `codex sandbox`
preflight to fail. The same named profile governs model-executed commands.
Ubuntu, GNU tools and `sudo apt-get` are required; AppArmor/Bubblewrap setup and
its diagnostics live here because the action promises this isolation. The real
Codex sandbox preflight is the runtime check; `bwrap --version` only logs a version.

Hosted ChatGPT connectors are disabled, configuration is strict, and sessions are
ephemeral. These are distinct from caller controls on GitHub permissions and
untrusted text. JSON framing helps separate data from instructions; it is not a
security boundary or a guarantee against misleading model recommendations.

## Verified baseline and bench

The previous head `fb614b9d` was validated with CLI `0.153.4` and `gpt-6-astra`:
[production smoke](https://github.com/homeassistant-ai/ha-mcp/actions/runs/34009384382),
[bench smoke](https://github.com/homeassistant-ai/ha-mcp-workflows-dev/actions/runs/34009096643),
[issues](https://github.com/homeassistant-ai/ha-mcp-workflows-dev/actions/runs/34009098004),
[PRs](https://github.com/homeassistant-ai/ha-mcp-workflows-dev/actions/runs/34009099607),
and [regression/timeout contracts](https://github.com/homeassistant-ai/ha-mcp-workflows-dev/actions/runs/34009095227).
These links document that baseline, not subsequent changes to the caller contract.
The old CLI `0.151.0` cannot run Astra and is not current validation evidence.

The [permanent bench](https://github.com/homeassistant-ai/ha-mcp-workflows-dev/blob/master/fixtures/README.md)
checks out an explicit canonical SHA and exercises the new contract with its own
credentials. The new report workflows become dispatchable in `ha-mcp` after they
land on its default branch; use the bench before merge. See its dated validation
record for exact revisions, runs and the authenticated gh scenario.
