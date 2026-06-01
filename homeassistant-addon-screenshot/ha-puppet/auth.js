// Pure auth-resolution helpers (no filesystem / process.env side effects), so
// the security-sensitive token selection can be unit-tested in isolation
// (see const.test.mjs). const.js imports these and applies them to the real
// options file + environment at module load.

/**
 * Whether the add-on should fall back to its Supervisor token: only in add-on
 * mode, only when no explicit access_token is configured, and only when a
 * Supervisor token is actually present.
 */
export function shouldUseSupervisorAuth({ isAddOn, accessToken, supervisorToken }) {
  return isAddOn && !accessToken && !!supervisorToken;
}

/**
 * Resolve the HA token: an explicit access_token always wins; otherwise fall
 * back to the Supervisor token in add-on auto-auth mode; otherwise undefined.
 */
export function resolveAuth({ isAddOn, accessToken, supervisorToken }) {
  return (
    accessToken ||
    (shouldUseSupervisorAuth({ isAddOn, accessToken, supervisorToken })
      ? supervisorToken
      : undefined)
  );
}
