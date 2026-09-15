# Maintainer slash coding workflow

An exact `/astra <task>` or `/sol <task>` comment from a repository maintainer
starts coding work on an issue or a same-repository PR. Actual `maintain` and
`admin` roles are checked through GitHub; `write`, `triage`, author association,
quoted commands and bot comments do not authorize a task. Manual dispatch names
an existing command comment and verifies both the dispatcher and rerunning actor.

The model mapping is fixed: Astra uses `gpt-6-astra`, Sol uses `gpt-5.6-sol`.
An issue starts `agents/issue-N` from the default branch and creates a draft PR.
A command on an existing same-repository PR works on its current branch. Forks,
the default/base branch, protected branches and unowned pre-existing agent
branches are rejected. The controller always appends a commit with one parent
and updates an existing ref with `force: false`. It never merges, enables
auto-merge, deletes branches, or requests a reviewer.

## Execution and credentials

The default-branch workflow has three jobs:

1. Admission independently rereads GitHub state, verifies authority and prepares
   an immutable plan artifact. It has read-only GitHub permissions and no secrets.
2. A coding worker checks out the admitted SHA, runs Codex and relevant tests,
   and packages changed files plus a structured report. The trusted controller
   checkout and `.git` are read-only in the Codex permission profile. Shell and
   command network are enabled; `gh` receives only the read-only job token.
   OAuth remains in the generic action's denied credential directory. Hosted
   apps and web search are disabled. Refreshed OAuth is saved under `always()`.
3. Publication runs on a fresh runner, checks out trusted default-branch code,
   reads the plan/result as data, and mints the App token after the worker exits.
   It revalidates the command, roles, source, branch/head and review threads,
   creates Git objects through the API, and reconciles the PR and checkpoint.
   It never executes generated source with the App credential.

The App needs Contents write, Pull requests write, Issues write, and Actions,
Checks and Commit statuses read (Metadata read is implicit). It needs no
Workflows, Administration or ruleset bypass permission. The intake workflow
continues requesting its narrower issue-documentation token from the same App.
Use the existing HA_MCP_APP_ID/HA_MCP_APP_SLUG variables and
HA_MCP_APP_PRIVATE_KEY/CODEX_AUTH/CODEX_AUTH_PAT secrets. Product and bench retain
separate Codex OAuth credentials.

The worker may change at most 80 regular files totalling 2 MiB. Publication
rejects traversal, symlinks/submodules, credential paths and `.github/`, `.codex/`
or `.claude/` changes. Workflow/agent infrastructure changes require a separate
human-controlled PR with the appropriate permissions. The read-only gh token is
available to model commands; only the OAuth and publication credentials are
isolated from those commands.

## Continuation, reviews and readiness

An App-owned checkpoint comment stores the task, chosen model, linked branch/PR,
last processed head, review digest, iteration count and compact continuation
notes. For issue-originated PRs, the PR body points back to that checkpoint.
The App's identity and the session/PR association are checked on every run.
Each new worker reconstructs context from GitHub and the checkpoint. This is
durable task persistence, not a restored Codex CLI transcript; credentials and
raw transcripts are never uploaded. Plan/result artifacts expire after one day,
and later work does not depend on them.

Maintainer comments/reviews and the existing CodeRabbit/Codex review bots can
resume an authorized session. A secretless review-event workflow wakes the trusted
controller; its completion is only a signal, not trusted instructions or an
artifact to execute. CI workflow completions and status changes are also signals.
The controller fetches current checks and feedback itself. Old issue-bot guesses
are excluded; review suggestions are hypotheses to validate, including collapsed
review bodies. The model can propose evidence-backed replies and resolution only
for supplied, unresolved threads from maintainers or the supported review bots.

Pending checks do not consume a coding turn. A new failing head or new feedback
can trigger another turn. A session gets at most four automatic coding turns per
command. A new task or `/astra resume` (or `/sol resume`) resets that budget;
`/astra pause` or `/sol pause` stops continuation. Revoking maintainer authority
also stops admission. Failed workers and exhausted budgets leave an explicit
blocked checkpoint, requiring a new maintainer command.

Readiness is deterministic: current checks must be complete and successful,
all required contexts must be present with the expected GitHub App where pinned,
all review threads must be resolved, and GitHub must report the PR mergeable.
The controller then marks a draft ready. This does not supply or dismiss human
approval; branch rules still govern merging. Ambiguous PR associations, unknown
mergeability, missing checks, or unresolved threads wait for a later event or
explicit command.

GitHub has no transaction spanning comments, refs and PR creation. Publication
reserves an owned checkpoint first and refuses stale source or concurrent branch
updates. A failure after a partial write remains visible; inspect the branch/PR
and send a new command to reconcile it. Do not delete an ownership checkpoint
while its branch is in use. Pause or disable the workflow to stop new work;
avoid cancelling an active OAuth consumer before its auth-persistence step.

## Verification

The normal pytest lane runs the Node controller/packaging regressions and checks
workflow credential boundaries. Real model, sandbox and publication scenarios
belong in `ha-mcp-workflows-dev`, use its own secrets and manifest fixtures, and
never mutate product issues/PRs. The bench is manually dispatched, with no canary.
The runtime workflow becomes available only after its dependencies and this PR
reach the default branch and the App permissions are installed.
