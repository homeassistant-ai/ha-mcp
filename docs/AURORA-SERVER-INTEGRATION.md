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
- `POST /{transaction}/activate`
- `POST /{transaction}/reload`
- `POST /promote-home-command`
- `POST /rollback-home-command`

The only dashboard targets are `home-command-preview` and `home-command`.
Bootstrap creates the preview dashboard only when absent, hidden and admin-only;
a mismatched existing preview fails closed. Bootstrap and staging never mutate
production.

## Verification and journal

Stage accepts only the signed canonical manifest, the deterministic Aurora
component archive, and the dashboard JavaScript bytes. The adapter verifies the
Ed25519 signature, key prefix, issue/expiry window, nonce replay state, fixed
release, fixed target, SHA-256 values, archive member policy, symlink/hardlink
absence, expanded-size/file-count bounds, and privacy denylist before writing a
fresh revision directory.

The durable journal is stored under the adapter-owned `.storage` directory and
is updated with atomic replace plus fsync. Transaction states are
`verified`, `activated`, and `promoted`; failed operations leave production
unchanged. Readback rehashes the immutable staged files rather than trusting
journal claims. Only the immediately prior verified production revision is
retained for rollback.

## Promotion

Promotion accepts only the currently activated preview revision and a separately
signed validation receipt. The receipt binds the exact revision and all asset
hashes, fixed dashboard target, responsive results for mobile/kiosk/tablet/
laptop/desktop, physical validation, issue/expiry, an isolated validation
signer, `preview_config_sha256`, and `expected_production_config_sha256`.
Promotion CAS-checks both the production revision label and canonical dashboard
configuration bytes immediately before saving. The restart-required
`aurora_camera_ai` component is never reported as reloaded; `/reload` returns
`409 restart_required` until an approved Home Assistant restart is completed.
The server rejects missing, expired, malformed, replayed, or mismatched receipts.
Rollback has no caller-selected revision and restores only the stored immediate
prior production revision after config-byte CAS verification.

No endpoint returns tokens, credentials, URLs, paths, private artifact content,
or raw dashboard/package bytes. Responses contain bounded transaction IDs,
revision IDs, states, booleans, and SHA-256 evidence only.
