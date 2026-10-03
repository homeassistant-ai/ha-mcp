// The report gate: a bug report filed without an ha_report_issue report or a
// reason why there is none is labeled, warned and, after the grace period,
// closed. Runs from issue-intake.yml (gate-check/gate-apply) and the hourly
// report-gate.yml sweep (gate-expire-check/gate-expire-apply).
import { appendFileSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import {
  GitHub,
  collect,
  escapeRegExp,
  hasReport,
  isHuman,
  latestEvent,
  noReportHeading,
  reportHeading,
  roleFor,
} from "./intake.mjs";

export const gateMarker = "<!-- ha-mcp-report-gate -->";
// The label that starts the 24-hour clock. A maintainer removing it waives
// the requirement for that issue.
export const gateLabel = "missing bug report output";
export const gateGraceHours = 24;
// The issue forms' required report field. Anything typed there, "N/A"
// included, counts as an answer.
export const reportFieldLabel = "📋 ha_report_issue Report";
const answerHeadingLine = new RegExp(
  `^ {0,3}#{2,4} +(?:${escapeRegExp(noReportHeading)}|${escapeRegExp(reportFieldLabel)})[ \\t]*$`,
  "iu",
);
const fenceLine = /^ {0,3}(`{3,}|~{3,})/;
const headingLine = /^ {0,3}#{1,6}(?: |$)/;
// Every section under an answer heading, each ending at the next heading
// outside a code fence: logs and config pasted there carry "# " lines.
function answerSections(text) {
  const sections = [];
  let current = null;
  let fence = null;
  for (const line of (text || "").split(/\r?\n/)) {
    const marker = fenceLine.exec(line)?.[1];
    if (fence) {
      if (marker?.[0] === fence[0] && marker.length >= fence.length)
        fence = null;
    } else if (marker) {
      fence = marker;
    } else if (headingLine.test(line)) {
      current = answerHeadingLine.test(line) ? [] : null;
      if (current) sections.push(current);
      continue;
    }
    current?.push(line);
  }
  return sections.map((lines) => lines.join("\n"));
}
// GitHub's issue forms write "_No response_" for an optional field left
// empty, and a blank issue's template comments are not an answer.
const answered = (section) =>
  section
    .split(/<!--[\s\S]*?-->/)
    .join(" ")
    .replace(/_No response_/g, "")
    .trim() !== "";
const hasAnswer = (text) => answerSections(text).some(answered);
const gateExemptRoles = ["write", "maintain", "admin"];

// The reporter's own words: the issue body and their comments.
const reporterTexts = ({ issue, comments }) => [
  issue.body || "",
  ...comments
    .filter((c) => c.user?.login === issue.user.login)
    .map((c) => c.body || ""),
];
// Feature requests and documentation issues are not gated. An issue that
// becomes one after it was labeled loses the label instead of closing.
const exemptKind = (issue) =>
  issue.title.trim().toUpperCase().startsWith("[FEATURE]") ||
  issue.labels.some((l) => ["enhancement", "documentation"].includes(l.name));
const hasGateLabel = (issue) => issue.labels.some((l) => l.name === gateLabel);
const isLabelEvent = (e, kind) =>
  e.event === kind && e.label?.name === gateLabel;
const gateNotice = (snapshot, bot) =>
  snapshot.comments
    .filter(
      (c) =>
        c.user?.login === bot &&
        c.user.type === "Bot" &&
        c.body.startsWith(gateMarker),
    )
    .at(-1);

// A maintainer removed the label: the report is not required here.
export function waived({ events, roles }) {
  const removed = latestEvent(events, (e) => isLabelEvent(e, "unlabeled"));
  return !!removed && gateExemptRoles.includes(roles[removed.actor?.login]);
}

export function satisfied(snapshot) {
  return reporterTexts(snapshot).some((t) => hasReport(t) || hasAnswer(t));
}

export function gateAction(snapshot, bot, event) {
  const { issue, events, roles } = snapshot;
  const author = issue.user;
  if (!isHuman(author) || gateExemptRoles.includes(roles[author.login]))
    return null;
  if (exemptKind(issue)) {
    if (issue.state === "open") return hasGateLabel(issue) ? "exempt" : null;
    const closed = latestEvent(events, (e) => e.event === "closed");
    return closed?.actor?.login === bot && hasGateLabel(issue) ? "exempt" : null;
  }
  const done = satisfied(snapshot);
  // An issue moved in from the HACS mirror is asked once, never labeled or
  // closed: a reporter whose issue was closed would refile it on the mirror.
  if (events.some((e) => e.event === "transferred"))
    return !done &&
      issue.state === "open" &&
      event.action === "opened" &&
      !gateNotice(snapshot, bot)
      ? "request"
      : null;
  if (waived(snapshot)) return null;
  const labeled = hasGateLabel(issue);
  if (done) {
    const closed = latestEvent(events, (e) => e.event === "closed");
    if (issue.state === "closed" && closed?.actor?.login === bot)
      return "reopen";
    return issue.state === "open" && labeled ? "clear" : null;
  }
  // Only a new or reopened issue is labeled, so an edit never starts the
  // clock on an issue that predates the gate. A maintainer's reopen does not.
  if (
    issue.state === "open" &&
    !labeled &&
    (event.action === "opened" ||
      (event.action === "reopened" &&
        !gateExemptRoles.includes(event.senderRole)))
  )
    return "label";
  return null;
}

// The scheduled sweep: close a labeled issue once its grace period has run
// out, or clear the label when the gate missed the answer.
export function expireAction(snapshot, now) {
  const { issue, events } = snapshot;
  if (issue.state !== "open" || !hasGateLabel(issue)) return null;
  if (exemptKind(issue)) return "exempt";
  if (satisfied(snapshot)) return "clear";
  // A maintainer may apply the label by hand to start the same clock.
  const applied = latestEvent(events, (e) => isLabelEvent(e, "labeled"));
  if (!applied) return null;
  const elapsed = now - Date.parse(applied.created_at);
  return elapsed >= gateGraceHours * 3600 * 1000 ? "close" : null;
}

// Why an attempt did not count, so the reporter can fix what they posted
// instead of being told it is missing.
function gateHint(texts) {
  if (texts.some((t) => reportHeading.test(t)))
    return "It has the report's heading but not the rest of the report, such as its `ha-mcp Version` line. Paste the whole report, from the Auto-Generated heading to its end, as raw Markdown.";
  if (texts.some((t) => answerSections(t).length))
    return `The \`### ${noReportHeading}\` section is empty.`;
  return "";
}

const howTo = `Ask your AI agent to run \`ha_report_issue\` in the session where the problem happened and add the report it generates (its \`issue_body\`, starting with the Auto-Generated heading) to the issue description or a comment, with secrets removed. It records the ha-mcp version, install type, client, settings and tool calls that maintainers otherwise have to ask for.

If no report can exist, add a section headed \`### ${noReportHeading}\` and say why, for example that ha-mcp does not start or connect (include your startup logs and configuration, tokens removed) or that the problem was found by reading the code. "N/A" is enough.`;

export function gateComment(action, login, hint = "") {
  const extra = hint ? ` ${hint}` : "";
  switch (action) {
    case "reopen":
    case "clear":
      return `${gateMarker}\nThanks: the issue now includes an \`ha_report_issue\` report or a reason why there is none.\n`;
    case "exempt":
      return `${gateMarker}\nThis issue is not a bug report, so no \`ha_report_issue\` report is needed.\n`;
    case "close":
      return `${gateMarker}\n@${login}, this issue was closed automatically because no \`ha_report_issue\` report or reason was added within ${gateGraceHours} hours.${extra} Add one and the issue reopens automatically.\n\n${howTo}\n`;
    case "request":
      return `${gateMarker}\n@${login}, this issue was moved to this repository and does not include an \`ha_report_issue\` report. Please add it here rather than in the repository you filed it in.${extra}\n\n${howTo}\n`;
    default:
      return `${gateMarker}\n@${login}, this issue does not include an \`ha_report_issue\` report or a reason why there is none.${extra} It will be closed automatically in ${gateGraceHours} hours unless one is added. If this is a feature request, start the title with \`[FEATURE]\`.\n\n${howTo}\n`;
  }
}

// Retries a read or an idempotent write after a rate limit or server error,
// so one transient failure does not leave an issue ungated.
async function retrying(api, call, attempt = 0) {
  try {
    return await call();
  } catch (error) {
    if (
      attempt < 2 &&
      (error.status === 429 || (error.status >= 500 && error.status <= 599))
    ) {
      await (api.delay ?? delay)([5, 15][attempt] * 1000);
      return retrying(api, call, attempt + 1);
    }
    throw error;
  }
}

// collect() looks up the roles of the reporter and commenters; a waiver
// also needs the role of whoever removed the label.
async function gateSnapshot(api, repository, number) {
  const snapshot = await retrying(api, () => collect(api, repository, number));
  const removed = latestEvent(snapshot.events, (e) =>
    isLabelEvent(e, "unlabeled"),
  );
  const login = removed?.actor?.login;
  if (login && !(login in snapshot.roles))
    snapshot.roles[login] = await retrying(api, () =>
      roleFor(api, repository, login),
    );
  return snapshot;
}

async function applyGate(api, repository, number, bot, snapshot, action) {
  const base = `repos/${repository}/issues`;
  const login = snapshot.issue.user.login;
  const body = gateComment(action, login, gateHint(reporterTexts(snapshot)));
  const post = () =>
    api.request(`${base}/${number}/comments`, {
      method: "POST",
      data: { body },
    });
  const idempotent = (path, options) =>
    retrying(api, () => api.request(path, options));
  const unlabel = async () => {
    if (!snapshot.issue.labels.some((l) => l.name === gateLabel)) return;
    try {
      await idempotent(`${base}/${number}/labels/${encodeURIComponent(gateLabel)}`, {
        method: "DELETE",
      });
    } catch (error) {
      if (error.status !== 404) throw error;
    }
  };
  // New notices are POSTed once, never retried: a POST that failed late may
  // still have been created. Each label or close notice is a new comment so
  // the mention notifies again; clear and reopen rewrite the latest notice.
  if (action === "label") {
    await post();
    await idempotent(`${base}/${number}/labels`, {
      method: "POST",
      data: { labels: [gateLabel] },
    });
  } else if (action === "close") {
    await post();
    await idempotent(`${base}/${number}`, {
      method: "PATCH",
      data: { state: "closed", state_reason: "not_planned" },
    });
  } else if (action === "request") {
    await post();
  } else {
    if (snapshot.issue.state === "closed")
      await idempotent(`${base}/${number}`, {
        method: "PATCH",
        data: { state: "open" },
      });
    await unlabel();
    const notice = gateNotice(snapshot, bot);
    if (notice)
      await idempotent(`${base}/comments/${notice.id}`, {
        method: "PATCH",
        data: { body },
      });
    else await post();
  }
}

export async function gate(api, repository, number, bot, event, write) {
  const snapshot = await gateSnapshot(api, repository, number);
  const action = gateAction(snapshot, bot, event);
  if (action && write)
    await applyGate(api, repository, number, bot, snapshot, action);
  return action;
}

// One pass of the scheduled sweep over every open labeled issue.
export async function expire(api, repository, bot, now, write) {
  const issues = await retrying(api, () =>
    api.request(
      `repos/${repository}/issues?labels=${encodeURIComponent(gateLabel)}&state=open&per_page=100`,
      { paginate: true },
    ),
  );
  const acted = [];
  for (const { number, pull_request } of issues) {
    if (pull_request) continue;
    const snapshot = await gateSnapshot(api, repository, number);
    const action = expireAction(snapshot, now);
    if (!action) continue;
    if (write) await applyGate(api, repository, number, bot, snapshot, action);
    acted.push(`${number}:${action}`);
  }
  return acted;
}

export async function main(command, options = {}) {
  const api = options.api ?? new GitHub();
  const env = options.env ?? process.env;
  const bot = `${env.HA_MCP_APP_SLUG}[bot]`;
  if (!/^[a-z0-9-]+\[bot\]$/.test(bot) || bot.startsWith("undefined"))
    throw Error("HA_MCP_APP_SLUG is required");
  const apply = command.endsWith("-apply");
  if (apply && env.TOKEN_APP_SLUG !== env.HA_MCP_APP_SLUG)
    throw Error("Installation token belongs to a different App");
  if (command === "gate-check" || command === "gate-apply") {
    const event = JSON.parse(readFileSync(env.GITHUB_EVENT_PATH, "utf8"));
    let action = null;
    if (!event.issue?.pull_request && isHuman(event.sender)) {
      // Only a reopen consults the sender's role.
      const senderRole =
        event.action === "reopened"
          ? await roleFor(api, env.GITHUB_REPOSITORY, event.sender.login)
          : null;
      // Both steps decide from current state: the apply step rereads it with
      // the write token, so an edit between them is not overwritten.
      action = await gate(
        api,
        env.GITHUB_REPOSITORY,
        event.issue.number,
        bot,
        { action: event.action, senderRole },
        apply,
      );
    }
    if (!apply)
      appendFileSync(env.GITHUB_OUTPUT, `action=${action ?? "none"}\n`);
    console.log(`Report gate: ${action ?? "no action"}`);
  } else if (command === "gate-expire-check" || command === "gate-expire-apply") {
    const now = options.now ?? Date.now();
    const acted = await expire(api, env.GITHUB_REPOSITORY, bot, now, apply);
    if (!apply) appendFileSync(env.GITHUB_OUTPUT, `due=${acted.length}\n`);
    console.log(`Report gate sweep: ${acted.join(", ") || "nothing due"}`);
  } else
    throw Error(
      "Expected gate-check, gate-apply, gate-expire-check or gate-expire-apply",
    );
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
  main(process.argv[2]).catch((error) => {
    const message = String(error.message).replace(/[\r\n]/g, " ");
    console.error(`::error::${message}`);
    process.exitCode = 1;
  });
}
