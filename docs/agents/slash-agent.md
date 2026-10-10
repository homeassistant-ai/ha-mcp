# Maintainer slash coding workflow

An exact `/astra <task>`, `/sol <task>` or `/terra <task>` comment from a
repository maintainer starts agent work on an issue or a same-repository PR. Actual `maintain` and
`admin` roles are checked through GitHub; `write`, `triage`, author association,
quoted commands and bot comments do not authorize a task. Manual dispatch names
an existing command comment and verifies both the dispatcher and rerunning actor.
The current body and last editor are fetched together through GraphQL. Edited
commands use the editor's current role, so write-role users cannot borrow the
original author's maintainer authority. Missing editor metadata fails closed.
This is necessary because [GitHub allows write-role collaborators to edit other
people's comments](https://docs.github.com/en/communities/moderating-comments-and-conversations/managing-disruptive-comments#editing-a-comment).
The command explicitly authorizes this lifecycle through readiness, including
publication and review replies. A separate ready command is not needed; pause
remains available before the readiness transition.

The model mapping is explicit: `/sol` and the compatibility alias `/astra` use
`gpt-6.1-sol`; `/terra` uses `gpt-5.6-terra`. Former `gpt-6-astra`, `gpt-6-sol`
and `gpt-5.6-sol` checkpoints remain readable. A continuation selects the
current mapping; no model is chosen dynamically from a catalog. Terra remains
subject to ChatGPT-account availability; an unavailable model blocks the
worker and requires a new command with a supported model.
An issue starts `agents/issue-N` from the default branch and creates a draft PR
when the result changes the repository. An unchanged result instead completes
in the App-owned issue checkpoint, whose
summary is the public answer, without creating a branch or PR. A blocked result
uses the same comment to explain the maintainer input it needs.
A command on an existing same-repository PR works on its current branch. Forks,
the default/base branch, protected branches and unowned pre-existing agent
branches are rejected. The controller always appends a commit with one parent
and updates an existing ref with `force: false`. It never merges, enables
auto-merge, deletes branches, or requests a reviewer.

## Execution and credentials

The entry workflow first routes an authorized event to its canonical session
root: the issue for an issue-originated PR, or the PR itself for an existing
PR command. Routing is read-only and does not reserve model work. Unrelated
automatic events stop here. All event types for that root then share one
queued call to `slash-agent-session.yml`; the lock covers all three jobs below,
including publication. Admission rereads the event and GitHub state after the
lock is acquired, so a duplicate event cannot spend a turn on a cached plan.
Distinct roots have separate queues; unrelated status bursts cannot fill a
maintainer's command queue.

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

The dedicated `ha-mcp-agent` App needs Contents write, Pull requests write,
Issues write, and Actions, Checks and Commit statuses read (Metadata read is
implicit). It needs no Workflows, Administration or ruleset bypass permission.
Configure the `HA_MCP_AGENT_CLIENT_ID` and `HA_MCP_AGENT_APP_SLUG` variables and
the `HA_MCP_AGENT_APP_PRIVATE_KEY` secret for this workflow. The intake workflow
continues using the narrower `ha-mcp` App and its existing `HA_MCP_APP_*`
configuration. Product and bench retain separate App private keys, Codex OAuth
credentials and `CODEX_AUTH_PAT` secrets.

This is repository-owned automation for maintainer authorization, model
selection and the tested issue-to-PR/review lifecycle. Workers use the one
Codex OAuth account configured in the repository's `CODEX_AUTH`, with no
per-maintainer account selection. Its `codex-auth` queue is also shared by
issue intake and report workflows to avoid simultaneous OAuth refreshes.
That limits throughput: slash jobs may wait behind these consumers, and the
58-minute worker job timeout is a ceiling, not a measured typical duration.
Concurrent maintainer capacity has not been load-tested.

The worker may change at most 80 regular files totalling 2 MiB. Publication
rejects traversal, symlinks/submodules, credential paths, root/scoped `AGENTS.md`
and `CLAUDE.md`, and `.github/`, `.codex/` or `.claude/` changes. These protected
entrypoints require a separate
human-controlled PR with the appropriate permissions. The read-only gh token is
available to model commands; only the OAuth and publication credentials are
isolated from those commands.

## Continuation, reviews and readiness

An App-owned checkpoint comment stores the task, chosen model, linked branch/PR,
last processed head, review digest, iteration count and compact continuation
notes. For issue-originated PRs, the PR body points back to that checkpoint.
The App's identity and the session/PR association are checked on every run.
An edited checkpoint must still have the App as its last editor. Manual edits
to checkpoint contents are rejected; use slash commands for changes instead.
Each new worker reconstructs context from GitHub and the checkpoint. This is
durable task persistence, not a restored Codex CLI transcript; credentials and
raw transcripts are never uploaded. The previous summary, test evidence and
continuation memory are bounded to 2,000/1,000/2,000 characters and replaced
after each model round. The worker also receives the current task, issue/PR,
human conversation, review feedback, checks and source checkout. Repository
decisions that must survive later rounds should be documented in the code or
durable project documentation, rather than relying on an internal transcript.
A failed coding job records only the private
log's byte count and recognized event counts in Actions; the log disappears with
the runner. Plan/result artifacts expire after one day, and later work does not
depend on them.

Substantive maintainer reviews/inline feedback and the existing CodeRabbit/Codex review bots can
resume an authorized session. A secretless review-event workflow wakes the trusted
controller; its completion is only a signal, not trusted instructions or an
artifact to execute. CI workflow completions and status changes are also signals.
Unrelated PRs without an App checkpoint exit before editor, review, role and
check collection. The controller fetches current checks and feedback itself for
owned sessions, and limits the resulting worker prompt to 512 KiB. Old issue-bot guesses
are excluded; review suggestions are hypotheses to validate, including collapsed
review bodies. The model can propose evidence-backed replies and resolution only
for supplied, unresolved threads containing maintainer or supported review-bot
feedback, even when someone else opened the thread. Clarifications are posted
before blocking, and review rounds receive one summary on the PR itself.

Pending or successful checks on a new head do not consume a coding turn,
including after readiness. A newly observed failing check or new authorized
review feedback can trigger another turn. Duplicate failures are remembered
by head and failed-check identity. Readiness can update the checkpoint without
calling the model. Ordinary PR discussion, approvals, empty reviews and the
Codex account-setup notice do not request coding; use a new slash command for
work requested through an ordinary comment. Reporter edits and ordinary text
remain context but cannot spend a turn through a later CI/status event. A session
gets at most four automatic coding turns per
command. A new task or a matching `/astra resume`, `/sol resume` or `/terra
resume` resets that budget; the corresponding `pause` command stops
continuation. Revoking maintainer authority
also stops admission. Failed workers and exhausted budgets leave an explicit
blocked checkpoint, requiring a new maintainer command.

Readiness is deterministic: current checks must be complete and successful,
all required contexts must be present with the expected GitHub App where pinned,
all review threads must be resolved, and GitHub must report the PR mergeable.
The controller then marks a draft ready. This does not supply or dismiss human
approval; branch rules still govern merging. Ambiguous PR associations, unknown
mergeability, missing checks, or unresolved threads wait for a later event or
explicit command.
After a clarification-only round, the publisher reevaluates readiness immediately
so a final thread resolution does not depend on another CI notification.

GitHub has no transaction spanning comments, refs and PR creation. Publication
reserves an owned checkpoint first and refuses stale source or concurrent branch
updates. Replies use stable markers tied to the command, code head and external
feedback; retries reuse their App-owned reply. The pending review-summary key
survives a failure during resolution, summary creation or final checkpoint save,
so recovery does not duplicate replies or summaries. A partial publication stays
resumable within the iteration budget; inspect a failure and send a new command
when intervention is needed. Stale work publishes no worker output and fails
its Actions run with the changed snapshot fields. If the command, authority and
checkpoint are still current, the discarded attempt is counted once against
the budget and stays in `retry` until a fresh worker completes or the budget
is exhausted; a newer command/session or revoked authority is never overwritten.
App-owned PR descriptions update their marked
section while preserving generated review sections. Do not delete an ownership checkpoint
while its branch is in use. Pause or disable the workflow to stop new work;
avoid cancelling an active OAuth consumer before its auth-persistence step.

## Verification

The normal pytest lane runs the Node controller/packaging regressions and checks
workflow credential boundaries. Real model, sandbox and publication scenarios
belong in `ha-mcp-workflows-dev`, use its own secrets and manifest fixtures, and
never mutate product issues/PRs. The bench is manually dispatched, with no canary.
The runtime workflow becomes available only after its dependencies and this PR
reach the default branch and the App permissions are installed.

## Review corrections — 2026-10-10

Feedback is remembered per authorized review/comment revision. Resolving,
deleting or dismissing a handled item does not spend a coding turn; a new or
edited item does. Older aggregate-only checkpoints retain the conservative
hash comparison until their next publication records item fingerprints.

Admission and publication share the same command parser and ordering. Tasks
are limited to 12,000 UTF-8 bytes, including JSON escaping (excluding the two
outer quotes), to prevent escaped text from consuming the checkpoint budget.
Checkpoint and public-comment sizes are still checked independently.

A new explicit command on a session whose issue or PR is closed/merged records
a visible refusal without a model call. It does not reopen the session; start
a separate request on an open issue or PR. Automatic events remain idle.
The worker instructions explicitly prohibit all `.env`/`.env.*` templates.
Managed PR descriptions update their delimited section even when a maintainer
has inserted a note above it; surrounding text remains unchanged.
