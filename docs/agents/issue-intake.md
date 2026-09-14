# Issue documentation

`issue-intake.yml` replaces CodeRabbit's issue enrichment and automatic labels.
The policy agreed in the maintainers discussion on 2026-09-13 is to document
reports, translate non-English reports, and request essential missing details.
The model must not diagnose, propose fixes, assign blame or priority, promise a
PR, or close an issue. CodeRabbit and Codex PR reviews keep their existing roles.

## Execution and permissions

Human issue openings, edits, reopenings, comment creations/edits/deletions, and removals of `needs-info`
trigger collection. PR comments and bot events are excluded. Collection uses the
workflow's read-only GitHub token, paginates comments and label history, and
checks actual repository roles. The `maintain` role must be read from
`role_name`; the legacy `permission` field maps it to `write`.

Codex receives only the issue and human conversation, with source IDs and
verified maintainer roles. It uses Terra with low reasoning by default. Manual
dispatch can select Terra or Sol. Luna remains bench-only: its #2404 trial asked
six irrelevant environment questions despite understanding the approved scope.
The action has read-only sandboxing,
shell disabled, hosted web search disabled, no command network, no GitHub token
and no enabled hosted apps.
The output is schema-validated; evidence quotes must exist in the cited source,
and agreed scope needs a maintainer citation. This prevents fabricated citations,
but it does not prove that a paraphrase is faithful; the model bench tests that.

After Codex exits, `actions/create-github-app-token` creates an installation
token restricted to the current repository with Issues write and Contents read
(Metadata read is implicit). No Administration, workflow writes, PR writes,
organization membership, or rule bypass is needed for this phase. Configure:

- variable `HA_MCP_APP_ID`: numeric GitHub App ID;
- variable `HA_MCP_APP_SLUG`: verified App slug;
- secret `HA_MCP_APP_PRIVATE_KEY`: private key of the installed App;
- the repository's existing, distinct `CODEX_AUTH` and `CODEX_AUTH_PAT`.

Do not transfer Codex OAuth credentials between product and bench. All consumers
share the repository's `codex-auth-...` concurrency group, without cancellation.
Every step is bounded, leaving time to save refreshed auth on failure. The model
has exited before publication credentials enter the environment.

## Public conversation and maintainer control

One App-owned comment contains the current summary, translation if useful,
agreed scope, extracted environment details, and targeted questions. Source links distinguish reported claims
from independently verified facts. The collector ignores previous bot theories,
including historical `ghhamcp` comments. The model cannot create arbitrary labels
or questions: the publisher maps missing field IDs to fixed questions.

Maintainers (repository `maintain` or `admin`) can post an exact comment:

- `/triage pause`: stop automated documentation for this issue;
- `/triage resume`: resume and clear prior manual needs-info suppression;
- `/triage refresh`: request a new pass without clearing pause or label overrides.

These controls are read from the human conversation on every run. Contributor
or reporter text cannot authorize them. Removing `needs-info` manually also
prevents the bot from adding it again until a maintainer explicitly resumes.
The bot removes only its own `needs-info` label, never a human-applied one.

An automatic `needs-info` request **does not start an automatic close clock**.
Only a human maintainer's label application permits `close-needs-info.yml` to
send reminders on days 3, 5 and 6, then close on day 7 without a reporter reply.
To confirm an existing bot request, remove and reapply the label. The generic
inactivity workflow excludes `needs-info`, so it cannot bypass this distinction.

Before publishing, the workflow fetches the current conversation again. A new
reply, closure, lock, or maintainer control invalidates the old model result;
the queued event will process the new state. An identical source fingerprint
does not call the model again. A pending marker keeps interrupted label writes
retryable without creating a second comment. Label/final-patch failures with
HTTP 429 or 5xx are retried twice with bounded backoff, rechecking human context
before each attempt. Exhausted failures remain pending and fail the run for
manual recovery; they must never be marked complete while a label write failed.
Failed day-seven closures retain needs-info for the next daily run.
GitHub does not provide a
transaction spanning comments and labels; a narrow concurrent human write can
still race the final API calls, so all publication remains scoped and reversible.

## Testing and operation

Run `node --test tests/js/issue-intake.test.mjs` for deterministic behavior and
the corresponding pytest wrapper in the normal unit lane. The permanent
`ha-mcp-workflows-dev` bench owns model fixtures and manual live-run validation.
KP13's #2404 scenario must recover the automation-only scope approved in the
comments, rather than implementing the broader opening request. It is source
data for bench tests, never authorization to modify the production issue.

No persistent Codex session is needed for documentation: each run reconstructs
the human conversation. The later, maintainer-invoked `/astra` and `/sol` coding
lifecycle is a separate phase requiring additional permissions and tests.

Disable `issue-intake.yml` to stop new runs; `/triage pause` handles one issue.
Inspect failed runs and retry via manual dispatch after correcting credentials
or malformed data. Oversized conversations fail visibly instead of silently
discarding late scope changes. Raw Codex logs and issue-context artifacts are
not automatically published. Do not restore CodeRabbit enrichment while intake
is active. Rollback can disable intake before restoring the earlier CodeRabbit
configuration, retaining the maintainer-confirmed close policy.
