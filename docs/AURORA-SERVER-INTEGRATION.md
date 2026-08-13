# Aurora deployment adapter server contract

This document defines the Home Assistant-side half of `ha-cli aurora`. It is a
separate `custom_components/aurora_deploy` integration and is not implemented
through `ha-mcp`, File Editor, Supervisor filesystem APIs, SSH, or generic
uploads.

## Installation boundary

1. Install the reviewed `aurora_deploy` component and its manifest through the
   approved Aurora add-on/custom-component release.
2. Place the server-managed trust file at the fixed Home Assistant config path
   `aurora_deploy_trusted_keys.json`. It maps `release-*` and `validation-*`
   key IDs to base64 Ed25519 public keys. The file is operator-managed; the
   deployment API cannot modify it.
3. Restart Home Assistant and verify that the component registers exactly two
   authenticated view families:
   `/api/aurora/deploy-preview/v1/{operation}` and
   `/api/aurora/deploy-preview/v1/{transaction_id}/{operation}`.
4. The route is accessed with the existing Home Assistant authentication
   boundary and requires an administrator user. It is not exposed through
   Supervisor ingress.

## Fixed contract

Only these operations exist:

- `POST /bootstrap`
- `POST /stage`
- `GET /{transaction}/readback`
- `GET /{operation_id}/status`
- `GET /{operation_id}/readback`
- `POST /{transaction}/activate`
- `POST /{transaction}/rollback`
- `POST /{transaction}/reload`
- `POST /promote-home-command`
- `POST /rollback-home-command`

The only dashboard targets are `aurora-preview` and `home-command`.
`home-command-preview` is a refused legacy collision, not a third environment.
Bootstrap creates the preview dashboard only when absent, hidden and admin-only;
an existing preview is accepted without registry mutation only for the canonical
`Aurora Preview`/`mdi:aurora` display pair or the exact historical
`Aurora V9 Preview`/`mdi:home-analytics` pair. Its URL path must remain
`aurora-preview`, hidden, and admin-only; all other values for these guarded
fields fail closed. Bootstrap and staging never mutate production.

## Verification and journal

Stage accepts only the signed canonical manifest, the deterministic Aurora
compatibility archive, and the dashboard JavaScript bytes. The adapter verifies the
Ed25519 signature, key prefix, issue/expiry window, nonce replay state, fixed
release, fixed target, SHA-256 values, archive member policy, symlink/hardlink
absence, expanded-size/file-count bounds, and privacy denylist before writing a
fresh revision directory. The archive remains staged evidence only: this adapter
never installs, replaces, reloads, or rolls back `custom_components/aurora_camera_ai`.
The Camera AI backend has its own release and restart lifecycle.
The closed reviewed source allowlist includes the runtime-imported
`custom_components/aurora_camera_ai/synthetic_fixture.py`; any undeclared archive
member remains rejected.

Stage requires a caller-known safe `transaction_id`. Before writing artifact
bytes, the journal records a prepared stage transition bound to the exact
manifest/package/dashboard request and reserves the manifest nonce. An exact
retry is idempotent, reuse with different bytes is rejected, and transaction
readback settles a response lost before, during, or after the artifact writes as
final `verified` or `aborted`. Manifest nonces remain globally replay-protected.

The durable journal is stored under the adapter-owned `.storage` directory and
is updated with atomic replace plus fsync. Transaction states include `staging`,
`verified`, `activated`, `reloaded`, `promoted`, `rolled_back`, and final
`aborted` recovery outcomes. Readback rehashes the immutable staged files rather
than trusting journal claims. Activation first verifies that any prior preview
revision and resource are journal-bound and content-addressed. Activation and
reload operate only on the fixed preview dashboard resource; both truthfully
return `restart_required: false` and `backend_unchanged: true`. Reload is a
verified no-op for already active dashboard assets. Only the immediately prior
verified production revision is retained for rollback.

Transaction readback runs under the transaction lock and reconciles a prepared
preview activation or preview rollback from the independently loaded live config.
It reports `activation_status` or `rollback_status` as `committed`/`aborted`, plus
the nullable `previous_revision`. `dashboard_resource_present` is always explicit;
when true, transaction readback returns `active_dashboard_resource_url`,
`active_dashboard_sha256`, and `active_dashboard_size` after rehashing the file.
A committed preview rollback independently
reloads the prior config and rehashes its content-addressed asset when a prior
revision exists. Activation, reload, and preview rollback are idempotent after a
committed response, so clients can keep mutation retries disabled and resolve an
ambiguous response through readback without repeating a save.

## Promotion

Promotion inspection returns the active preview revision and transaction ID,
manifest/package/dashboard hashes, preview and expected-production config hashes,
production revision, strict same-origin resource URL/hash/size, and a stable
non-secret Home Assistant audience persisted in the journal. It names both sides
explicitly as `dashboard_target: aurora-preview` and
`target_dashboard: home-command`.

Inspection is the non-mutating `POST /promote-home-command` request
`{"preview_revision":"<revision>","inspect":true}`. Its fixed resource fields
are `preview_resource_url`, `preview_resource_sha256`, and
`preview_resource_size`; the URL is exactly
`/local/aurora/revisions/aurora-preview-dashboard-<dashboard_sha256>.js`.

Promotion accepts only the currently activated preview revision and a separately
signed validation receipt. Schema v1 remains the physical-device receipt format.
Schema v2 is the automated E2E format and contains no `physical_validation` claim.
It binds `validation_kind: automated_e2e`, `action: promote_home_command`, the
inspection audience and transaction, a caller-generated `operation_id`, an E2E
evidence SHA-256, and exactly these
six passing screenshot profiles: `mobile-390x844`,
`tablet-portrait-800x1280`, `tablet-landscape-1280x800`, `kiosk-1280x800`,
`laptop-1100x800`, and `desktop-1440x1000`. Each profile record contains only
`profile_id`, `width`, `height`, `passed`, and `screenshot_sha256`. V2 receipts
expire no more than ten minutes after issue. Both schemas use canonical compact,
key-sorted JSON with `signature` omitted for Ed25519 verification and require a
`validation-*` signer. There is no additional signature-domain prefix.

Schema v1 keeps its physical receipt unchanged, but the promotion request now
requires a caller-known canonical RFC 4122 UUID in body `operation_id`. The exact
v1 request is `preview_revision`, `expected_production_revision`, `operation_id`,
and `receipt`. Exact committed/aborted replay of that operation is idempotent;
reusing the UUID with a different candidate, CAS value, or canonical receipt is
rejected. Schema v2 retains its signed safe 8–128 character `operation_id`, which
must also equal the body value.

Promotion CAS-checks both the production revision label and canonical dashboard
configuration bytes immediately before saving. It persists an operation ID before
the write, independently reloads and hashes production before committing success,
and exposes read-only status/readback by operation ID. An exact retry of the same
committed v2 operation is idempotent; reusing its ID with different signed
receipt/evidence is rejected. The server rejects missing, expired, malformed,
replayed, or mismatched receipts.

Operation status and readback acquire the same journal lock and settle their own
prepared promotion/rollback transition before returning. Readback verifies the
final committed or aborted revision/config state. A committed promotion additionally
requires production to reference the exact content-addressed
`/local/aurora/revisions/aurora-preview-dashboard-<sha256>.js` module and
independently rehashes that file against the transaction-bound SHA-256 and size.
Aborted promotion readback likewise verifies any content-addressed resource in
the expected production prestate. Missing, tampered, or substituted resources
fail closed. Committed readback fields use `dashboard_resource_url`,
`dashboard_sha256`, and `dashboard_size`, plus `applied: true` and
`verified: true`. Operation readback always reports
`dashboard_resource_present`; an aborted operation returns `applied: false` only
after the original CAS state and optional resource are independently verified.

Production rollback has no caller-selected destination. Its request must bind a
caller-known `operation_id`, `expected_current_revision`, and
`expected_current_config_sha256`; the server checks both CAS values against the
journal and live production, durably prepares the operation, restores only the
stored immediate prior revision, and independently reloads and hashes the result
before committing the rollback journal update. Exact committed retries are
idempotent and status/readback use the same operation ID. Rollback status and
readback expose `expected_current_revision` and
`expected_current_config_sha256` aliases so the caller can bind reconciliation
to its request. Both the expected-current and restored configs rehash their
content-addressed assets when present.

No endpoint returns tokens, credentials, filesystem paths, private artifact
content, raw dashboard/package bytes, or caller-controlled/external URLs. The
only returned URL is the strictly validated fixed same-origin dashboard resource.
Other responses contain bounded transaction IDs, revision IDs, states, booleans,
and SHA-256 evidence only.
