// Unit tests for the security-sensitive auth resolution.
// Run with: node --test
//
// The resolver lives in auth.js (side-effect-free) precisely so it can be
// imported and tested without const.js's import-time options-file read /
// process.exit.
import assert from "node:assert/strict";
import { test } from "node:test";

import { resolveAuth, shouldUseSupervisorAuth } from "./auth.js";

test("configured access_token always wins, even with a supervisor token", () => {
  assert.equal(
    resolveAuth({
      isAddOn: true,
      accessToken: "user-llat",
      supervisorToken: "sup-token",
    }),
    "user-llat",
  );
});

test("add-on mode with empty token + supervisor token uses the supervisor token", () => {
  assert.equal(
    resolveAuth({ isAddOn: true, accessToken: "", supervisorToken: "sup-token" }),
    "sup-token",
  );
  assert.equal(
    shouldUseSupervisorAuth({
      isAddOn: true,
      accessToken: "",
      supervisorToken: "sup-token",
    }),
    true,
  );
});

test("dev mode (isAddOn=false) never uses the env supervisor token", () => {
  assert.equal(
    resolveAuth({ isAddOn: false, accessToken: "", supervisorToken: "sup-token" }),
    undefined,
  );
});

test("empty/undefined supervisor token guard yields undefined, not empty string", () => {
  assert.equal(
    resolveAuth({ isAddOn: true, accessToken: "", supervisorToken: "" }),
    undefined,
  );
  assert.equal(
    resolveAuth({ isAddOn: true, accessToken: "", supervisorToken: undefined }),
    undefined,
  );
});
