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
  // An agent's feature request filed without the prefix learns how to exempt it.
  assert.match(api.writes[0].data.body, /start the title with `\[FEATURE\]`/);
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
    [(s) => (s.issue.body += `\n\n${section(noReportHeading.toLowerCase(), "N/A")}`), null],
    [(s) => (s.issue.body += `\n\n${section(reportFieldLabel, "N/A")}`), null],
    // Levels two to four count, indented by up to three spaces.
    [(s) => (s.issue.body += `\n\n## ${noReportHeading}\n\nN/A`), null],
    [(s) => (s.issue.body += `\n\n#### ${noReportHeading}\n\nN/A`), null],
    [(s) => (s.issue.body += `\n\n   ### ${noReportHeading}\n\nN/A`), null],
    // Other ATX forms GitHub renders as the same heading.
    [(s) => (s.issue.body += `\n\n### ${noReportHeading} ###\n\nN/A`), null],
    [(s) => (s.issue.body += `\n\n###\t${noReportHeading}\n\nN/A`), null],
    [(s) => (s.issue.body += `\n\n### ${noReportHeading}\t###\n\nN/A`), null],
    [(s) => (s.issue.body += `\n\n### ${noReportHeading}\t\n\nN/A`), null],
    [(s) => (s.issue.body += `\n\n### ${noReportHeading} #\n\nN/A`), null],
    // Pasted config opens the reason; its comment line is not a heading.
    [(s) => (s.issue.body += `\n\n${reason("```\n# uvx ha-mcp\n```")}`), null],
    // A closing fence may be indented or end in a tab.
    [(s) => (s.issue.body += `\n\n\`\`\`\nlog\n  \`\`\`\n\n${reason("N/A")}`), null],
    [(s) => (s.issue.body += `\n\n\`\`\`\nlog\n\`\`\`\t\n\n${reason("N/A")}`), null],
    // A line opening with inline code is no fence that could hide the reason.
    [(s) => (s.issue.body += `\n\n\`\`\`ha_get_state\`\`\` returns 500.\n\n${reason("N/A")}`), null],
    // Nor is an issue reference opening the reason.
    [(s) => (s.issue.body += `\n\n${reason("#2788 has the same trace, found by reading the code.")}`), null],
    // Template comments around a reason leave it in place, and a "<!--" in
    // text, code or a fence's info string hides nothing after it.
    [(s) => (s.issue.body += `\n\n<!-- What went wrong? -->\nThe call hangs.\n\n${reason("ha-mcp does not start.")}\n\n<!-- Any other relevant information: -->`), null],
    [(s) => (s.issue.body += `\n\n${reason("Found by reading the code. <!-- from the form -->")}`), null],
    [(s) => (s.issue.body += `\n\n${reason("<!-- Explain here -->\nFound by reading the code.\n<!-- Optional -->")}`), null],
    [(s) => (s.issue.body += `\n\n${reason("N/A <!-- a --><!-- b -->")}`), null],
    [(s) => (s.issue.body += `\n\n${reason("<!---->N/A")}`), null],
    [(s) => (s.issue.body += `\n\n\`\`\`\n<!-- broken\n\`\`\`\n\n${reason("N/A")}\n\n<!-- template -->`), null],
    [(s) => (s.issue.body += `\n\n\`\`\`<!--\ncode\n\`\`\`\n\n${reason("N/A")}\n\n<!-- template -->`), null],
    [(s) => (s.issue.body += `\n\nI saw <!-- in the body.\n\n${reason("N/A")}\n\n<!-- template -->`), null],
    [(s) => (s.issue.body += `\n\nThe form leaves \`<!--\` behind.\n\n${reason("N/A")}\n\n<!-- template -->`), null],
    [(s) => (s.issue.body += `\n\n<!-->\n${reason("N/A")}\n\n<!-- template -->`), null],
    // Only a comment opening a line hides the lines after it, a fence included;
    // one opened after text on its line is text up to the next heading, and so
    // is one indented as code, "<!-" or text after the closing "-->".
    [(s) => (s.issue.body += `\n\nThe call hangs <!-- see below\n${reason("N/A")}\n\n-->`), null],
    [(s) => (s.issue.body += `\n\n<!--\n\`\`\`\n-->\n\n${reason("N/A")}`), null],
    [(s) => (s.issue.body += `\n\n    <!--\n${reason("N/A")}`), null],
    [(s) => (s.issue.body += `\n\n\t<!--\n${reason("N/A")}`), null],
    [(s) => (s.issue.body += `\n\n<!- x\n${reason("N/A")}`), null],
    [(s) => (s.issue.body += `\n\n<!-- template --> see below\n${reason("N/A")}`), null],
    // Text after the "-->" that ends such a comment shows, and so does an
    // unclosed fence's content.
    [(s) => (s.issue.body += `\n\n${reason("<!-- Explain here --> Found by reading the code.")}`), null],
    [(s) => (s.issue.body += `\n\n${reason("<!--\nExplain here\n--> Found by reading the code.")}`), null],
    [(s) => (s.issue.body += `\n\n${reason("```\nha-mcp exits before the handshake")}`), null],
    // A code block shows a comment in it as text.
    [(s) => (s.issue.body += `\n\n${reason("```html\n<!-- x -->\n```")}`), null],
    [(s) => (s.issue.body += `\n\n${reason("```\n<!-- broken\n```\nFound by reading the code.")}`), null],
    // A heading inside a comment that opens a line does not end the reason
    // section.
    [(s) => (s.issue.body += `\n\n### ${noReportHeading}\n\n_No response_\n\n<!--\n### Additional context\n-->\nnone`), null],
    // What an issue form writes for an empty optional field.
    [(s) => (s.issue.body += `\n\n${reason("_No response_")}`), "label"],
    // A heading with a tab after its hashes, or an empty one, ends the reason
    // section too.
    [(s) => (s.issue.body += `\n\n### ${noReportHeading}\n\n_No response_\n\n###\tAdditional context\n\nnone`), "label"],
    [(s) => (s.issue.body += `\n\n### ${noReportHeading}\n\n_No response_\n\n###\n\nmore`), "label"],
    // A blank issue's template comment is not the reporter's answer, nor is a
    // heading inside a comment or a code block the page shows as code.
    [(s) => (s.issue.body += `\n\n${reason("<!-- Explain here -->")}`), "label"],
    [(s) => (s.issue.body += `\n\n<!--\n${reason("Explain why here")}\n-->`), "label"],
    [(s) => (s.issue.body += `\n\n   <!-- template\n${reason("Explain why here")}`), "label"],
    [(s) => (s.issue.body += `\n\n\`\`\`\nlog\n\`\`\`yaml\n${reason("in the log")}\n\`\`\``), "label"],
    // Such a comment runs over blank lines, a ">" and a "->", and may open
    // right after a paragraph line.
    [(s) => (s.issue.body += `\n\n<!--\n\n${reason("Explain why here")}\n-->`), "label"],
    [(s) => (s.issue.body += `\n\n<!-- a > b\n${reason("Explain why here")}\n-->`), "label"],
    [(s) => (s.issue.body += `\n\n<!-- a -> b\n${reason("Explain why here")}\n-->`), "label"],
    [(s) => (s.issue.body += `\n\nThe call hangs.\n<!-- see below\n${reason("Explain why here")}\n-->`), "label"],
    // A reason of nothing but comments is none.
    [(s) => (s.issue.body += `\n\n${reason("<!--\nExplain here\n-->")}`), "label"],
    [(s) => (s.issue.body += `\n\n${reason("<!-- a --><!-- b -->")}`), "label"],
    [(s) => (s.issue.body += `\n\n${reason("<!-- e.g. Settings > Integrations -->")}`), "label"],
    // A comment left open hides the rest of the text, and an empty code block
    // shows no text.
    [(s) => (s.issue.body += `\n\n${reason("<!-- Explain here")}`), "label"],
    [(s) => (s.issue.body += `\n\n${reason("<!-- Explain here\nwhy there is no report")}`), "label"],
    [(s) => (s.issue.body += `\n\n${reason("```\n```")}`), "label"],
    [(s) => (s.issue.body += `\n\n${reason("~~~ yaml\n\n~~~")}`), "label"],
    // A shorter or other-kind fence line closes no fence, and a tilde fence's
    // info string may hold backticks.
    [(s) => (s.issue.body += `\n\n\`\`\`\`\n\`\`\`\n${reason("in the log")}\n\`\`\`\``), "label"],
    [(s) => (s.issue.body += `\n\n\`\`\`\n~~~\n${reason("in the log")}\n\`\`\``), "label"],
    [(s) => (s.issue.body += `\n\n~~~ \`x\`\n${reason("in the log")}\n~~~`), "label"],
    // Nor is the text after a comment's closing "-->" a heading.
    [(s) => (s.issue.body += `\n\n<!-- x --> ### ${noReportHeading}\n\nN/A`), "label"],
    // A fifth level, or a heading indented as code, is not the reason heading.
    [(s) => (s.issue.body += `\n\n##### ${noReportHeading}\n\nN/A`), "label"],
    [(s) => (s.issue.body += `\n\n    ### ${noReportHeading}\n\nN/A`), "label"],
    // Text after the label, a "#" included, makes it another heading.
    [(s) => (s.issue.body += `\n\n### ${noReportHeading}#\n\nN/A`), "label"],
    [(s) => (s.issue.body += `\n\n### ${noReportHeading} (reading the code)\n\nN/A`), "label"],
    [(s) => (s.issue.body += `\n\n### ${noReportHeading} ## more\n\nN/A`), "label"],
    // Naming the report's heading in prose is no report.
    [(s) => (s.issue.body += ` The ${reportMarker} heading is missing.`), "label"],
    // The heading over a summary, or a paste cut off before the environment.
    [(s) => (s.issue.body = `## 🚨 ${reportMarker}\n\nThe call hangs.`), "label"],
    [(s) => s.comments.push(comment(1, "bystander", report)), "label"],
    // A report inside a comment the page hides, closed or not.
    [(s) => (s.issue.body += `\n\n<!--\n${report}\n-->`), "label"],
    [(s) => (s.issue.body += `\n\n<!--\n${report}`), "label"],
    // The text after a closing "-->" shows, but never as a heading.
    [(s) => (s.issue.body += `\n\n<!-- x --> ${report}`), "label"],
  ];
  const mismatches = cases.flatMap(([edit, expected]) => {
    const s = snapshot();
    edit(s);
    const actual = gateAction(s, bot, opened);
    return actual === expected ? [] : [{ edit: edit.toString(), expected, actual }];
  });
  assert.deepEqual(mismatches, []);
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

test("a labeled issue that becomes a feature request loses the label instead of closing", () => {
  const at = Date.parse("2026-09-20T00:00:00Z");
  const retitled = labeled();
  retitled.issue.title = "[FEATURE] Manage Thread datasets";
  const relabeled = labeled();
  relabeled.issue.labels.push({ name: "enhancement" });
  for (const s of [retitled, relabeled]) {
    assert.equal(gateAction(s, bot, edited), "exempt");
    assert.equal(expireAction(s, at + 48 * hour), "exempt");
  }
});

test("an issue the sweep closed reopens when it becomes a feature request; one a human closed does not", async () => {
  const s = closedByGate();
  s.issue.title = "[FEATURE] Manage Thread datasets";
  const api = new FakeGitHub(s);
  assert.equal(await gate(api, "test/repo", 1, bot, edited, true), "exempt");
  assert.deepEqual(writes(api), [
    ["PATCH", "repos/test/repo/issues/1"],
    ["DELETE", `repos/test/repo/issues/1/labels/${encodeURIComponent(gateLabel)}`],
    ["PATCH", "repos/test/repo/issues/comments/5"],
  ]);
  assert.deepEqual(api.writes[0].data, { state: "open" });
  const byHuman = closedByGate();
  byHuman.issue.title = "[FEATURE] Manage Thread datasets";
  byHuman.events.push({
    id: 8,
    event: "closed",
    actor: user("maintainer"),
    created_at: "2026-09-23T00:00:00Z",
  });
  assert.equal(gateAction(byHuman, bot, edited), null);
});

test("clearing the label of an issue that became a feature request says no report is needed", async () => {
  const s = labeled();
  s.issue.title = "[FEATURE] Manage Thread datasets";
  const api = new FakeGitHub(s);
  assert.equal(await gate(api, "test/repo", 1, bot, edited, true), "exempt");
  assert.deepEqual(writes(api), [
    ["DELETE", `repos/test/repo/issues/1/labels/${encodeURIComponent(gateLabel)}`],
    ["PATCH", "repos/test/repo/issues/comments/5"],
  ]);
  assert.match(api.writes[1].data.body, /not a bug report, so no `ha_report_issue` report is needed/);
});

test("answering a labeled issue clears the label and rewrites the warning", async () => {
  const s = labeled();
  s.issue.body += `\n\n${reason("N/A")}`;
  const api = new FakeGitHub(s);
  assert.equal(await gate(api, "test/repo", 1, bot, edited, true), "clear");
  assert.deepEqual(writes(api), [
    ["DELETE", `repos/test/repo/issues/1/labels/${encodeURIComponent(gateLabel)}`],
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
    ["DELETE", `repos/test/repo/issues/1/labels/${encodeURIComponent(gateLabel)}`],
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
  const { acted } = await expire(
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
  assert.match(api.writes[0].data.body, /reopens automatically/);
});

test("the sweep closes a labeled issue that has no user, without promising a reopen", async () => {
  const s = labeled();
  s.issue.user = null;
  // Without a reporter login, a reason in a reply by a null user is not theirs.
  s.comments.push(comment(6, "nobody", reason("Found by reading the code."), { user: null }));
  const api = new FakeGitHub(s);
  const { acted } = await expire(api, "test/repo", bot, Date.parse("2026-09-21T00:00:00Z"), true);
  assert.deepEqual(acted, ["1:close"]);
  assert.match(api.writes[0].data.body, /@ghost, this issue was closed automatically/);
  // gateAction() skips an issue without a user, so no answer reopens it.
  assert.doesNotMatch(api.writes[0].data.body, /reopens/);
});

test("an issue the gate never reopens closes without promising a reopen", async () => {
  // Labeled by a maintainer's hand: the gate itself never labels these.
  const byHand = () => {
    const s = snapshot();
    s.issue.labels.push({ name: gateLabel });
    s.events.push(labelEvent("labeled", "maintainer", "2026-09-20T00:00:00Z"));
    return s;
  };
  const maintainers = byHand();
  maintainers.issue.user = user("maintainer");
  const moved = byHand();
  moved.events.unshift({ id: 1, event: "transferred", actor: user("maintainer"), created_at: "2026-09-19T00:00:00Z" });
  // A label a maintainer removed once waives the report for good.
  const relabeled = byHand();
  relabeled.events.unshift(labelEvent("unlabeled", "maintainer", "2026-09-19T00:00:00Z"));
  for (const [name, s] of Object.entries({ maintainers, moved, relabeled })) {
    const api = new FakeGitHub(s);
    const { acted } = await expire(api, "test/repo", bot, Date.parse("2026-09-21T00:00:00Z"), true);
    assert.deepEqual(acted, ["1:close"], name);
    assert.doesNotMatch(api.writes[0].data.body, /reopens/, name);
    // Answered afterwards, it indeed stays closed.
    s.issue.state = "closed";
    s.events.push({ id: 9, event: "closed", actor: { login: bot, type: "Bot" }, created_at: "2026-09-21T00:00:00Z" });
    s.issue.body += `\n\n${reason("N/A")}`;
    assert.equal(gateAction(s, bot, edited), null, name);
  }
});

test("one failing issue does not stop the sweep, and the apply step names it", async (t) => {
  const s = labeled();
  const failing = (api) => {
    const request = api.request.bind(api);
    api.request = (path, options = {}) => {
      if (path.includes("/issues?")) return [{ number: 2 }, { number: 1 }];
      if (path.includes("/issues/2")) throw TypeError("broken");
      return request(path, options);
    };
    return api;
  };
  const at = Date.parse("2026-09-21T00:00:00Z");
  const api = failing(new FakeGitHub(s));
  assert.deepEqual(await expire(api, "test/repo", bot, at, true), {
    acted: ["1:close"],
    failed: ["#2: broken"],
  });
  assert.deepEqual(writes(api), [
    ["POST", "repos/test/repo/issues/1/comments"],
    ["PATCH", "repos/test/repo/issues/1"],
  ]);
  // The check step counts it as due, so the apply step runs and fails on it,
  // and warns on one line, so the error text starts no workflow command.
  const failingWith = (message) => (api) => {
    failing(api);
    const request = api.request.bind(api);
    api.request = (path, options = {}) => {
      if (path.includes("/issues/2")) throw TypeError(message);
      return request(path, options);
    };
    return api;
  };
  const log = t.mock.method(console, "log", () => {});
  const check = await runCommand("gate-expire-check", {}, s, "ha-mcp", at, failingWith("broken\n::error::x"));
  const lines = log.mock.calls.map((c) => String(c.arguments[0]));
  log.mock.restore();
  assert.equal(check.output, "due=2\n");
  assert.deepEqual(lines.filter((l) => l.startsWith("::")), ["::warning::Report gate sweep failed for #2: broken ::error::x"]);
  await assert.rejects(
    runCommand("gate-expire-apply", {}, s, "ha-mcp", at, failing),
    /Report gate sweep failed for #2: broken/,
  );
});

test("a warning says when a report heading came without the rest of the report", async () => {
  for (const [body, shown] of [
    [`## 🚨 ${reportMarker}\n\nThe call hangs.`, true],
    // A heading the page hides is no heading it shows.
    [`<!--\n## 🚨 ${reportMarker}\n-->`, false],
    [`<!-- x --> ## 🚨 ${reportMarker}`, false],
  ]) {
    const s = snapshot();
    s.issue.body = body;
    const api = new FakeGitHub(s);
    await gate(api, "test/repo", 1, bot, opened, true);
    assert.equal(/heading but not the rest/.test(api.writes[0].data.body), shown, body);
  }
});

test("a warning names the answer section that came empty", async () => {
  for (const [heading, expected] of [
    [noReportHeading, noReportHeading],
    [`${noReportHeading} ###`, noReportHeading],
    [reportFieldLabel, reportFieldLabel],
  ]) {
    const s = snapshot();
    s.issue.body = section(heading, "_No response_");
    const api = new FakeGitHub(s);
    await gate(api, "test/repo", 1, bot, opened, true);
    assert.ok(api.writes[0].data.body.includes(`\`### ${expected}\` section is empty`), heading);
  }
});

test("a warning says when a comment hides the answer or the report", async () => {
  for (const [body, expected] of [
    [reason("<!-- Explain here\nFound by reading the code."), `The text under \`### ${noReportHeading}\` sits inside an HTML comment`],
    [section(reportFieldLabel, "<!--\nN/A"), `The text under \`### ${reportFieldLabel}\` sits inside an HTML comment`],
    // A closed comment hides typed text too.
    [reason("<!-- Found by reading the code. -->"), `The text under \`### ${noReportHeading}\` sits inside an HTML comment`],
    [`<!--\n${report}\n-->`, "The report sits inside an HTML comment"],
    // A form whose report field holds only a comment, its reason left empty.
    [`${section(reportFieldLabel, "<!-- N/A -->")}\n\n${reason("_No response_")}`, `The text under \`### ${reportFieldLabel}\` sits inside an HTML comment`],
    // A template's commented-out heading is not text under the reason, and an
    // empty comment hides nothing.
    [`${reason("_No response_")}\n\n<!--\n### Logs\n-->`, `\`### ${noReportHeading}\` section is empty`],
    [reason("<!--"), `\`### ${noReportHeading}\` section is empty`],
  ]) {
    const s = snapshot();
    s.issue.body = body;
    const api = new FakeGitHub(s);
    await gate(api, "test/repo", 1, bot, opened, true);
    assert.ok(api.writes[0].data.body.includes(expected), body);
  }
});

test("the check steps decide without writing", async () => {
  const api = new FakeGitHub(snapshot());
  assert.equal(await gate(api, "test/repo", 1, bot, opened, false), "label");
  const sweep = new FakeGitHub(labeled());
  assert.deepEqual(
    (await expire(sweep, "test/repo", bot, Date.parse("2026-09-22T00:00:00Z"), false)).acted,
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

async function runCommand(command, event, s = snapshot(), slug = "ha-mcp", now, wrap = (api) => api) {
  const dir = mkdtempSync(join(tmpdir(), "gate-"));
  const eventPath = join(dir, "event.json");
  const outputPath = join(dir, "output");
  writeFileSync(eventPath, JSON.stringify(event));
  writeFileSync(outputPath, "");
  const api = wrap(new FakeGitHub(s));
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
