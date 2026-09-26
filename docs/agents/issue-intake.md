# Issue documentation

`issue-intake.yml` documents reports, translates non-English reports, and asks
for essential missing information. The model must not diagnose, propose fixes,
assign blame or priority, promise a PR, or close an issue. CodeRabbit issue
enrichment is disabled so this workflow owns the consolidated issue comment.
CodeRabbit and Codex PR reviews retain their existing responsibilities.

## Execution and permissions

The workflow has two jobs. Admission filters PR/bot activity and unrelated labels,
checks manual dispatch roles, and coalesces a short burst
of events per issue. Superseded admission jobs can be cancelled: they have no
Codex credentials. Only admitted jobs enter the shared `codex-auth-...` job queue.
An active OAuth consumer is never cancelled to coalesce comments, so refreshed
credentials can be persisted. Events already queued beyond admission are cheap
when their current source fingerprint has already been processed.

Collection runs after acquiring that queue. It fetches the issue, all human
comments and label events through `gh`, checks current repository roles, and
reconstructs the source context. `role_name` distinguishes maintain from write;
the legacy `permission` field does not. Issue opens, edits, reopenings, human
comment creations/edits/deletions, needs-info labeling/unlabeling and locking/
unlocking are subscribed. Locking pauses processing; unlocking allows it again.

Codex receives source IDs and verified maintainer annotations. Terra with low
reasoning is the default; manual dispatch can select Sol. The model runs in a
read-only sandbox with shell, hosted web search and command network disabled.
It receives neither a GitHub token nor the App private key. Hosted apps are off.

The result has a schema-enforced `needs_translation` boolean. Summaries and
translations cite source excerpts; matching normalizes CRLF to LF and ignores
`` ` `` and `*` markers unless they sit between two word characters, rejecting
quotes or values left empty. Each extracted
fact value must occur in its cited quote. `missing_fields` contains every
outstanding question. `already_requested` is a subset with evidence from a
maintainer, used only to avoid repeating that question. It never means answered.
Errors identify the failing field/index and known source ID without dumping
issue text. These checks establish provenance, not the truth of reported claims
or the semantic correctness of every paraphrase; the model bench evaluates that.

After the model exits, a short-lived installation token is minted for the current
repository with Issues write, Contents read and implicit Metadata read. The
publisher validates output and rereads current GitHub state before writing.
Configure these per-repository values:

- `HA_MCP_APP_ID` and `HA_MCP_APP_SLUG` variables;
- `HA_MCP_APP_PRIVATE_KEY` secret;
- the repository's distinct `CODEX_AUTH` and `CODEX_AUTH_PAT` secrets.

Do not transfer Codex OAuth credentials between product and bench. All Codex
callers share the repository's auth concurrency group. Step timeouts reserve
room for auth persistence on failure. No Administration, organization membership,
workflow/PR write permission, or ruleset bypass is needed for issue documentation.

## Conversation, control and lifecycle

One App-owned comment contains the summary, optional English translation,
agreed scope, reported details and targeted questions. Prior bot theories are
excluded from model input. The publisher owns question wording and can only
manage needs-info; it does not execute instructions from the report.

Maintainers with the actual maintain/admin role can post exact commands:

- `/triage pause`: stop automated documentation for this issue;
- `/triage resume`: resume and clear prior manual needs-info suppression;
- `/triage refresh`: request another pass, without overriding pause or labels.

Unauthorized exact commands do not control the workflow and are excluded from
model context. Their event remains admissible so it can process earlier human
activity that it may have coalesced; an unchanged fingerprint avoids a redundant
model call. Ordinary human comments still trigger documentation. Removing needs-info manually prevents
the intake publisher from reapplying it until an explicit maintainer resume.
The publisher removes only its own label, never a human-applied label.

The separate `close-needs-info.yml` lifecycle applies to **every** needs-info
issue, regardless of who applied the label. It counts from the newest label
event, sends reminders on days 3/5/6 and closes on day 7 without a reporter reply.
A reporter reply after labeling clears needs-info regardless of label ownership;
a maintainer/bystander reply does not. The generic inactivity workflow excludes
needs-info because this seven-day workflow already owns its close path. PRs are
explicitly excluded from issue closure.

Clearing needs-info acknowledges the reporter's reply; it does not assert that
every requested field was answered. If later human activity still leaves an
essential field missing, intake may apply needs-info again. That new label event
starts a fresh seven-day cycle.

The publisher rereads context once at the start of each publication attempt,
before its owned-comment and label write sequence. Relevant source changes
invalidate an old result and emit a workflow warning; a subscribed event processes
the new state, or an operator can refresh explicitly. A lock or pause intentionally
stops progress until reversed. Identical source fingerprints avoid another model
call. A pending marker preserves incomplete writes for recovery, without another
comment. Transient patches to an existing owned comment and idempotent label
writes retry twice after fixed 5-second and 15-second delays.
Exhausted failures stay pending and fail visibly; failed work is never marked done.
GitHub has no transaction spanning these writes, so narrow concurrent changes can
still race individual API calls.

Failed closures retain needs-info for a subsequent daily retry. Their closing
notice is deduplicated within the label cycle. Failed issue reads, reminders,
closing notices, reply cleanup and post-close cleanup fail the batch after other
issues are processed; failures identify the affected issues.

## Testing and operation

The normal pytest lane runs the dependency-free `tests/js/issue-intake*.test.mjs`
behavior suites. Model fixtures, case-specific expectations and live-run results
belong to `ha-mcp-workflows-dev`. Tests must not publish reports on product issues.
Each documentation run reconstructs context; no persistent Codex session is needed.

Disable the workflow to stop all new runs, or pause one issue with its command.
Inspect failed-run diagnostics and dispatch a refresh after correcting credentials
or malformed data. Oversized conversations fail visibly instead of silently
losing late clarifications. Raw Codex logs and source artifacts are not published
automatically. Disable intake before restoring competing CodeRabbit enrichment
as a rollback; the existing needs-info lifecycle remains in effect.
