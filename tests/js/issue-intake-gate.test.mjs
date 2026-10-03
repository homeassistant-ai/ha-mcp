import test from "node:test";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert/strict";
import {
  FakeGitHub,
  bot,
  comment,
  snapshot,
  user,
} from "./issue-intake-helpers.mjs";
import {
  expire,
  expireAction,
  gate,
  gateAction,
  gateLabel,
  gateMarker,
  main,
  reportFieldLabel,
} from "../../.github/issue-intake/gate.mjs";
import {
  noReportHeading,
  reportMarker,
  reportVersionLine,
} from "../../.github/issue-intake/intake.mjs";

const opened = { action: "opened", senderRole: "read" };
const edited = { action: "edited", senderRole: "read" };
const report = `## 🚨 ${reportMarker}\n\n${reportVersionLine} 8.6.0`;
const section = (heading, text) =>
  `### ${heading}\n\n${text}\n\n### Additional context\n\nnone`;
const reason = (text) => section(noReportHeading, text);
const hour = 3600 * 1000;
const labelEvent = (kind, login, created_at = "2026-09-20T00:00:00Z") => ({
  id: kind === "labeled" ? 3 : 4,
  event: kind,
  label: { name: gateLabel },
  actor: login === bot ? { login: bot, type: "Bot" } : user(login),
  created_at,
});
const labeled = (s = snapshot()) => {
  s.issue.labels.push({ name: gateLabel });
  s.events.push(labelEvent("labeled", bot));
  s.comments.push(
    comment(5, bot, `${gateMarker}\nlabeled`, { user: { login: bot, type: "Bot" } }),
  );
  return s;
};
const closedByGate = () => {
  const s = labeled();
  s.issue.state = "closed";
  s.events.push({
    id: 6,
    event: "closed",
    actor: { login: bot, type: "Bot" },
    created_at: "2026-09-21T00:00:00Z",
  });
  return s;
};
const writes = (api) => api.writes.map((w) => [w.method, w.path]);

test("a bug report with neither a report nor a reason is labeled and warned, not closed", async () => {
  const api = new FakeGitHub(snapshot());
  assert.equal(await gate(api, "test/repo", 1, bot, opened, true), "label");
  assert.deepEqual(writes(api), [
    ["POST", "repos/test/repo/issues/1/comments"],
    ["POST", "repos/test/repo/issues/1/labels"],
  ]);
  assert.deepEqual(api.writes[1].data, { labels: [gateLabel] });
  assert.match(api.writes[0].data.body, /^<!-- ha-mcp-report-gate -->\n@reporter,/);
  assert.match(api.writes[0].data.body, /closed automatically in 24 hours/);
});

test("a report, or any answer in the report field or reason section, keeps an issue unlabeled", () => {
  const cases = [
    [(s) => (s.issue.body += `\n\n${report}`), null],
    [(s) => (s.issue.body = report.replace("🚨", "🤖")), null],
    // Pasted into the form field, or with the fence the tool shows it in.
    [(s) => (s.issue.body = section(reportFieldLabel, report)), null],
    [(s) => (s.issue.body = `\`\`\`\`markdown\n${report}\n\`\`\`\``), null],
    [(s) => s.comments.push(comment(1, "reporter", report)), null],
    [(s) => (s.issue.body += `\n\n${reason("N/A")}`), null],
    [(s) => (s.issue.body += `\n\n${section(reportFieldLabel, "N/A")}`), null],
    // Pasted config opens the reason; its comment line is not a heading.
    [(s) => (s.issue.body += `\n\n${reason("```\n# uvx ha-mcp\n```")}`), null],
    // What an issue form writes for an empty optional field.
    [(s) => (s.issue.body += `\n\n${reason("_No response_")}`), "label"],
    // A blank issue's template comment is not the reporter's answer.
    [(s) => (s.issue.body += `\n\n${reason("<!-- Explain here -->")}`), "label"],
    [(s) => (s.issue.body += ` The ${reportMarker} heading is missing.`), "label"],
    // The heading over a summary, or a paste cut off before the environment.
    [(s) => (s.issue.body = `## 🚨 ${reportMarker}\n\nThe call hangs.`), "label"],
    [(s) => s.comments.push(comment(1, "bystander", report)), "label"],
  ];
  for (const [edit, expected] of cases) {
    const s = snapshot();
    edit(s);
    assert.equal(gateAction(s, bot, opened), expected, edit.toString());
  }
});

test("feature requests, maintainers, bots and documentation issues are not gated", () => {
  const featureTitle = snapshot();
  featureTitle.issue.title = "[FEATURE] Manage Thread datasets";
  const featureLabel = snapshot();
  featureLabel.issue.labels.push({ name: "enhancement" });
  const writer = snapshot();
  writer.issue.user = user("writer");
  const app = snapshot();
  app.issue.user = { login: "coderabbitai[bot]", type: "Bot" };
  const docs = snapshot();
  docs.issue.labels.push({ name: "documentation" });
  for (const s of [featureTitle, featureLabel, writer, app, docs])
    assert.equal(gateAction(s, bot, opened), null);
});

test("answering a labeled issue clears the label and rewrites the warning", async () => {
  const s = labeled();
  s.issue.body += `\n\n${reason("N/A")}`;
  const api = new FakeGitHub(s);
  assert.equal(await gate(api, "test/repo", 1, bot, edited, true), "clear");
  assert.deepEqual(writes(api), [
    ["DELETE", `repos/test/repo/issues/1/labels/${gateLabel}`],
    ["PATCH", "repos/test/repo/issues/comments/5"],
  ]);
  assert.match(api.writes[1].data.body, /^<!-- ha-mcp-report-gate -->\nThanks/);
});

test("answering an issue the gate closed reopens it and clears the label", async () => {
  const s = closedByGate();
  s.issue.body += `\n\n${report}`;
  const api = new FakeGitHub(s);
  assert.equal(await gate(api, "test/repo", 1, bot, edited, true), "reopen");
  assert.deepEqual(writes(api), [
    ["PATCH", "repos/test/repo/issues/1"],
    ["DELETE", `repos/test/repo/issues/1/labels/${gateLabel}`],
    ["PATCH", "repos/test/repo/issues/comments/5"],
  ]);
  assert.deepEqual(api.writes[0].data, { state: "open" });
});

test("the gate never reopens an issue a maintainer closed", () => {
  const s = closedByGate();
  // Listed first but newer: the latest close decides, not array order.
  s.events.unshift({
    id: 7,
    event: "closed",
    actor: user("maintainer"),
    created_at: "2026-09-22T00:00:00Z",
  });
  s.issue.body += `\n\n${report}`;
  assert.equal(gateAction(s, bot, edited), null);
});

test("edits never label an existing issue, and only a non-maintainer's reopen does", () => {
  const s = snapshot();
  assert.equal(gateAction(s, bot, edited), null);
  assert.equal(gateAction(s, bot, { action: "reopened", senderRole: "maintain" }), null);
  assert.equal(gateAction(s, bot, { action: "reopened", senderRole: "read" }), "label");
});

test("a maintainer removing the label waives the report; the reporter removing it does not", async () => {
  const reopen = { action: "reopened", senderRole: "read" };
  for (const [login, expected] of [
    ["maintainer", null],
    ["reporter", "label"],
  ]) {
    const s = snapshot();
    s.events.push(labelEvent("labeled", bot), labelEvent("unlabeled", login, "2026-09-21T00:00:00Z"));
    // The remover's role is looked up even though they never commented.
    delete s.roles[login];
    const api = new FakeGitHub(s);
    api.data.roles[login] = login === "maintainer" ? "maintain" : "read";
    assert.equal(await gate(api, "test/repo", 1, bot, reopen, false), expected, login);
  }
});

test("the sweep closes an unanswered labeled issue only once 24 hours have passed", () => {
  const s = labeled();
  const at = Date.parse("2026-09-20T00:00:00Z");
  assert.equal(expireAction(s, at + 23 * hour), null);
  assert.equal(expireAction(s, at + 24 * hour), "close");
  const answered = labeled();
  answered.issue.body += `\n\n${report}`;
  assert.equal(expireAction(answered, at + 24 * hour), "clear");
  // A label a maintainer removed is gone from the issue; nothing to sweep.
  const unlabeled = snapshot();
  assert.equal(expireAction(unlabeled, at + 48 * hour), null);
});

test("the sweep's close posts a new notice and closes as not planned", async () => {
  const api = new FakeGitHub(labeled());
  const acted = await expire(
    api,
    "test/repo",
    bot,
    Date.parse("2026-09-21T00:00:00Z"),
    true,
  );
  assert.deepEqual(acted, ["1:close"]);
  assert.deepEqual(writes(api), [
    ["POST", "repos/test/repo/issues/1/comments"],
    ["PATCH", "repos/test/repo/issues/1"],
  ]);
  assert.deepEqual(api.writes[1].data, { state: "closed", state_reason: "not_planned" });
  assert.match(api.writes[0].data.body, /@reporter, this issue was closed automatically/);
});

test("a warning says when a report heading came without the rest of the report", async () => {
  const s = snapshot();
  s.issue.body = `## 🚨 ${reportMarker}\n\nThe call hangs.`;
  const api = new FakeGitHub(s);
  await gate(api, "test/repo", 1, bot, opened, true);
  assert.match(api.writes[0].data.body, /heading but not the rest/);
});

test("the check steps decide without writing", async () => {
  const api = new FakeGitHub(snapshot());
  assert.equal(await gate(api, "test/repo", 1, bot, opened, false), "label");
  const sweep = new FakeGitHub(labeled());
  assert.deepEqual(
    await expire(sweep, "test/repo", bot, Date.parse("2026-09-22T00:00:00Z"), false),
    ["1:close"],
  );
  assert.deepEqual([...api.writes, ...sweep.writes], []);
});

test("a transient GitHub error does not leave a new issue ungated", async () => {
  const api = new FakeGitHub(snapshot());
  const request = api.request.bind(api);
  let failures = 1;
  api.request = (path, options = {}) => {
    if (!options.method && failures-- > 0)
      throw Object.assign(Error("Service Unavailable"), { status: 503 });
    return request(path, options);
  };
  assert.equal(await gate(api, "test/repo", 1, bot, opened, true), "label");
  assert.deepEqual(api.delays, [5000]);
});

const transferredIn = () => {
  const s = snapshot();
  s.events.push({
    id: 1,
    event: "transferred",
    actor: user("maintainer"),
    created_at: "2026-09-20T00:00:00Z",
  });
  return s;
};

test("an issue moved in without a report is asked once, never labeled or closed", async () => {
  const api = new FakeGitHub(transferredIn());
  assert.equal(await gate(api, "test/repo", 1, bot, opened, true), "request");
  assert.deepEqual(writes(api), [["POST", "repos/test/repo/issues/1/comments"]]);
  assert.doesNotMatch(api.writes[0].data.body, /closed/);
  const asked = transferredIn();
  asked.comments.push(
    comment(5, bot, `${gateMarker}\nasked`, { user: { login: bot, type: "Bot" } }),
  );
  for (const event of [opened, { action: "reopened", senderRole: "read" }])
    assert.equal(gateAction(asked, bot, event), null);
});

async function runCommand(command, event, s = snapshot(), slug = "ha-mcp", now) {
  const dir = mkdtempSync(join(tmpdir(), "gate-"));
  const eventPath = join(dir, "event.json");
  const outputPath = join(dir, "output");
  writeFileSync(eventPath, JSON.stringify(event));
  writeFileSync(outputPath, "");
  const api = new FakeGitHub(s);
  await main(command, {
    api,
    now,
    env: {
      HA_MCP_APP_SLUG: "ha-mcp",
      TOKEN_APP_SLUG: slug,
      GITHUB_REPOSITORY: "test/repo",
      GITHUB_EVENT_PATH: eventPath,
      GITHUB_OUTPUT: outputPath,
    },
  });
  return { api, output: readFileSync(outputPath, "utf8") };
}

test("the gate judges a reopen by who reopened it, not who filed it", async () => {
  const reopened = (login) => ({
    action: "reopened",
    issue: { number: 1 },
    sender: user(login),
  });
  const byMaintainer = await runCommand("gate-check", reopened("maintainer"));
  assert.equal(byMaintainer.output, "action=none\n");
  const byReporter = await runCommand("gate-check", reopened("reporter"));
  assert.equal(byReporter.output, "action=label\n");
});

test("the sweep's check step reports how many issues are due", async () => {
  const { output } = await runCommand(
    "gate-expire-check",
    {},
    labeled(),
    "ha-mcp",
    Date.parse("2026-09-22T00:00:00Z"),
  );
  assert.equal(output, "due=1\n");
});

test("the apply steps refuse another App's token before writing", async () => {
  for (const command of ["gate-apply", "gate-expire-apply"])
    await assert.rejects(
      runCommand(
        command,
        { action: "opened", issue: { number: 1 }, sender: user("reporter") },
        snapshot(),
        "other-app",
      ),
      /different App/,
    );
});

test("every issue form asks for the report under the headings the gate reads", () => {
  for (const form of ["runtime_bug", "agent_behavior", "startup_bug", "feature_request"]) {
    const text = readFileSync(
      new URL(`../../.github/ISSUE_TEMPLATE/${form}.yml`, import.meta.url),
      "utf8",
    );
    // The report field's own validations, not a later field's.
    assert.match(
      text,
      new RegExp(`label: "${reportFieldLabel}"(?:(?!\\n {2}- )[\\s\\S])*?required: true`),
      form,
    );
    assert.ok(text.includes(`label: "${noReportHeading}"`), form);
  }
});
