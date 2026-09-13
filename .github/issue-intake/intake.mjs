import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import {
  readFileSync,
  writeFileSync,
  mkdirSync,
  appendFileSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
export const policy = readFileSync(resolve(here, "instructions.md"), "utf8");
export const marker = "<!-- ha-mcp-intake:v1 ";
export const questions = {
  install_method:
    "How is ha-mcp installed (HACS integration, app/add-on, Docker, uvx/pip, or another method)?",
  ha_mcp_version: "Which ha-mcp version are you using?",
  ha_version: "Which Home Assistant version are you using?",
  client: "Which AI client and client version are you using?",
  transport:
    "How does the client connect to ha-mcp (for example, direct HTTP, stdio, or a bridge)?",
  operating_system:
    "Which operating system runs the affected client or server?",
  affected_tool: "Which ha-mcp tool or operation is affected?",
  error:
    "What is the exact error message? Remove tokens and secret URLs before sharing logs.",
  reproduction:
    "What steps reproduce the behavior, and what did you expect instead?",
};
const fields = Object.keys(questions);
const string = (maxLength) => ({ type: "string", minLength: 1, maxLength });
const object = (properties) => ({
  type: "object",
  additionalProperties: false,
  properties,
  required: Object.keys(properties),
});
const array = (items, maxItems, minItems = 0) => ({
  type: "array",
  items,
  minItems,
  maxItems,
});
const evidence = array(
  object({ source_id: string(40), quote: string(700) }),
  5,
  1,
);
const statement = object({ text: string(1200), evidence });
export const schema = object({
  language: string(60),
  summary: array(statement, 5, 1),
  translation: array(statement, 8),
  agreed_scope: array(statement, 5),
  facts: array(
    object({
      field: { type: "string", enum: fields },
      value: string(300),
      evidence,
    }),
    fields.length,
  ),
  missing_fields: array({ type: "string", enum: fields }, fields.length),
  already_requested: array({ type: "string", enum: fields }, fields.length),
});

export function validateSchema(value, spec = schema, path = "result") {
  if (spec.type === "object") {
    if (!value || typeof value !== "object" || Array.isArray(value))
      throw Error(`${path}: expected object`);
    if (
      Object.keys(value).some((k) => !(k in spec.properties)) ||
      spec.required.some((k) => !(k in value))
    )
      throw Error(`${path}: unexpected or missing property`);
    for (const [k, v] of Object.entries(value))
      validateSchema(v, spec.properties[k], `${path}.${k}`);
  } else if (spec.type === "array") {
    if (
      !Array.isArray(value) ||
      value.length < spec.minItems ||
      value.length > spec.maxItems
    )
      throw Error(`${path}: invalid array length`);
    value.forEach((v, i) => validateSchema(v, spec.items, `${path}[${i}]`));
  } else if (
    typeof value !== "string" ||
    (spec.enum && !spec.enum.includes(value)) ||
    (spec.minLength && !value.trim()) ||
    value.length > (spec.maxLength ?? Infinity)
  ) {
    throw Error(`${path}: invalid string`);
  }
}

export function validateResult(result, context) {
  validateSchema(result);
  const sources = new Map(context.sources.map((s) => [s.source_id, s]));
  for (const item of [
    ...result.summary,
    ...result.translation,
    ...result.agreed_scope,
    ...result.facts,
  ]) {
    for (const e of item.evidence) {
      if (!sources.get(e.source_id)?.text.includes(e.quote) || !e.quote.trim())
        throw Error("Evidence must quote a supplied source exactly");
    }
  }
  for (const item of result.agreed_scope) {
    if (!item.evidence.some((e) => sources.get(e.source_id)?.maintainer))
      throw Error("Agreed scope requires maintainer evidence");
  }
  for (const values of [
    result.facts.map((f) => f.field),
    result.missing_fields,
    result.already_requested,
  ]) {
    if (new Set(values).size !== values.length) throw Error("Duplicate field");
  }
  if (result.facts.some((f) => result.missing_fields.includes(f.field)))
    throw Error("A field cannot be both known and missing");
  return result;
}

export class GitHub {
  request(endpoint, { method = "GET", data, paginate = false } = {}) {
    const args = ["api", endpoint, "--method", method];
    if (paginate) args.push("--paginate", "--slurp");
    if (data !== undefined) args.push("--input", "-");
    let output;
    try {
      output = execFileSync("gh", args, {
        input: data === undefined ? undefined : JSON.stringify(data),
        encoding: "utf8",
        maxBuffer: 8 * 1024 * 1024,
        timeout: 60000,
        stdio: ["pipe", "pipe", "pipe"],
      });
    } catch (error) {
      // Do not echo issue bodies, API payloads, or token-bearing stderr into logs.
      const status = /HTTP (\d+)/.exec(String(error.stderr ?? ""))?.[1];
      throw Object.assign(
        Error(`GitHub ${method} failed${status ? ` (HTTP ${status})` : ""}`),
        { status: Number(status) },
      );
    }
    if (!output.trim()) return null;
    const parsed = JSON.parse(output);
    if (!paginate) return parsed;
    if (!Array.isArray(parsed) || !parsed.every(Array.isArray))
      throw Error("Incomplete GitHub pagination");
    return parsed.flat();
  }
}

const isHuman = (user) => user?.type === "User" && user.login !== "ghhamcp";
const isMaintainer = (role) => ["maintain", "admin"].includes(role);
const timestamp = (item) => item.updated_at ?? item.created_at;

export function makeContext(snapshot) {
  const { issue, comments, roles } = snapshot;
  return {
    title: issue.title,
    sources: [
      {
        source_id: "body",
        text: `${issue.title}\n\n${issue.body || ""}`,
        url: issue.html_url,
        author: issue.user.login,
        maintainer: isMaintainer(roles[issue.user.login]),
      },
      ...comments
        .filter((c) => isHuman(c.user))
        .map((c) => ({
          source_id: `comment-${c.id}`,
          text: c.body,
          url: c.html_url,
          author: c.user.login,
          maintainer: isMaintainer(roles[c.user.login]),
        })),
    ],
  };
}

export function control(snapshot) {
  const commands = snapshot.comments.filter(
    (c) =>
      isHuman(c.user) &&
      isMaintainer(snapshot.roles[c.user.login]) &&
      /^\/triage (pause|resume|refresh)\s*$/.test(c.body),
  );
  commands.sort(
    (a, b) => timestamp(a).localeCompare(timestamp(b)) || a.id - b.id,
  );
  const stateCommand = commands
    .filter((c) => !c.body.trim().endsWith("refresh"))
    .at(-1);
  const resumedAt =
    stateCommand?.body.trim() === "/triage resume"
      ? timestamp(stateCommand)
      : "";
  const removed = snapshot.events.some(
    (e) =>
      e.event === "unlabeled" &&
      e.label?.name === "needs-info" &&
      isHuman(e.actor) &&
      e.created_at > resumedAt,
  );
  return {
    paused: stateCommand?.body.trim() === "/triage pause",
    labelOverride: removed,
  };
}

export function fingerprint(snapshot) {
  // Own comments and label writes must not cause endless retriage.
  const value = {
    policy,
    schema,
    context: makeContext(snapshot),
    state: snapshot.issue.state,
    locked: snapshot.issue.locked,
    control: control(snapshot),
    edits: snapshot.comments
      .filter((c) => isHuman(c.user))
      .map((c) => [c.id, timestamp(c)]),
    labelEvents: snapshot.events
      .filter((e) => e.label?.name === "needs-info" && isHuman(e.actor))
      .map((e) => [e.id, e.event, e.created_at]),
  };
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

export function ownComment(snapshot, bot) {
  const matches = snapshot.comments.filter(
    (c) =>
      c.user?.login === bot &&
      c.user.type === "Bot" &&
      c.body.startsWith(marker),
  );
  if (matches.length > 1)
    throw Error("Multiple intake comments require maintainer reconciliation");
  return matches[0];
}

export async function collect(api, repository, number) {
  if (
    !/^[\w.-]+\/[\w.-]+$/.test(repository) ||
    !Number.isSafeInteger(number) ||
    number < 1
  )
    throw Error("Invalid issue target");
  const base = `repos/${repository}/issues/${number}`;
  const issue = await api.request(base);
  if (issue.pull_request)
    throw Error("Issue intake cannot process a pull request");
  const comments = await api.request(`${base}/comments?per_page=100`, {
    paginate: true,
  });
  const events = await api.request(`${base}/events?per_page=100`, {
    paginate: true,
  });
  if (!Array.isArray(comments) || !Array.isArray(events))
    throw Error("Incomplete issue context");
  const roles = {};
  const users = [issue.user, ...comments.map((c) => c.user)];
  for (const login of new Set(users.filter(isHuman).map((u) => u.login))) {
    try {
      const permission = await api.request(
        `repos/${repository}/collaborators/${encodeURIComponent(login)}/permission`,
      );
      roles[login] = permission.role_name;
    } catch (error) {
      if (error.status !== 404) throw error;
      roles[login] = "none";
    }
  }
  const snapshot = { repository, issue, comments, events, roles };
  if (Buffer.byteLength(JSON.stringify(makeContext(snapshot))) > 160000)
    throw Error(
      "Issue exceeds intake context budget; maintainer review required",
    );
  return snapshot;
}

export function prepare(snapshot, bot, { force = false } = {}) {
  if (
    snapshot.issue.state !== "open" ||
    snapshot.issue.locked ||
    control(snapshot).paused
  )
    return { run: false, reason: "Closed, locked, or paused by a maintainer" };
  const digest = fingerprint(snapshot);
  const previous = ownComment(snapshot, bot);
  if (!force && previous?.body.startsWith(`${marker}${digest} -->`))
    return { run: false, reason: "Current human context already processed" };
  return { run: true, digest, context: makeContext(snapshot), snapshot };
}

export function prompt(context) {
  return `${policy}\n\nSOURCE DATA (JSON):\n${JSON.stringify(context)}\n`;
}

// Escape model prose so it cannot inject mentions, images or Markdown links.
export function prose(text) {
  return text
    .replace(/\s+/g, " ")
    .trim()
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/@/g, "&#64;")
    .replace(/([\\`*_{}\[\]()#!|])/g, "\\$1");
}

export function render(result, prepared) {
  validateResult(result, prepared.context);
  const sourceMap = new Map(
    prepared.context.sources.map((s) => [s.source_id, s]),
  );
  const line = (item) =>
    `- ${prose(item.text)} ${[...new Set(item.evidence.map((e) => e.source_id))].map((id) => `[source](${sourceMap.get(id).url})`).join(" ")}`;
  const lines = [
    `${marker}${prepared.digest} -->`,
    "## Issue summary",
    "",
    ...result.summary.map(line),
  ];
  if (result.translation.length)
    lines.push(
      "",
      "### English translation",
      "",
      ...result.translation.map(line),
    );
  if (result.agreed_scope.length)
    lines.push(
      "",
      "### Scope agreed in the discussion",
      "",
      ...result.agreed_scope.map(line),
    );
  const missing = result.missing_fields.filter(
    (f) => !result.already_requested.includes(f),
  );
  if (missing.length) {
    lines.push(
      "",
      "### Information needed",
      "",
      ...missing.map((f) => `- ${questions[f]}`),
    );
    lines.push(
      "",
      "You can reply here with these details. If ha-mcp is reachable, you can also ask your agent to run `ha_report_issue` and share its report after removing secrets.",
    );
  } else if (result.missing_fields.length) {
    lines.push(
      "",
      "A maintainer has already requested the remaining details above; please reply to that request.",
    );
  }
  lines.push(
    "",
    "*Automated documentation of this conversation; technical claims have not been independently verified.*",
  );
  return `${lines.join("\n")}\n`;
}

export function labelAction(snapshot, bot, result) {
  if (control(snapshot).labelOverride) return null;
  const present = snapshot.issue.labels.some((l) => l.name === "needs-info");
  if (result.missing_fields.length && !present) return "add";
  const applied = snapshot.events
    .filter((e) => e.event === "labeled" && e.label?.name === "needs-info")
    .sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id - b.id)
    .at(-1);
  if (
    !result.missing_fields.length &&
    present &&
    applied?.actor?.login === bot &&
    applied.actor.type === "Bot"
  )
    return "remove";
  return null;
}

export async function publish(api, prepared, result, bot) {
  const body = render(result, prepared);
  const latest = await collect(
    api,
    prepared.snapshot.repository,
    prepared.snapshot.issue.number,
  );
  if (
    fingerprint(latest) !== prepared.digest ||
    !prepare(latest, bot, { force: true }).run
  )
    return "Context changed; publication skipped";
  const existing = ownComment(latest, bot);
  const base = `repos/${latest.repository}/issues`;
  const action = labelAction(latest, bot, result);
  const pending = action
    ? body.replace(`${prepared.digest} -->`, `${prepared.digest} pending -->`)
    : body;
  // Updating one owned comment is idempotent. A failed POST is not retried here;
  // the next run recollects comments before deciding whether to create one.
  if (existing) {
    if (existing.body !== pending)
      await api.request(`${base}/comments/${existing.id}`, {
        method: "PATCH",
        data: { body: pending },
      });
  } else {
    const created = await api.request(
      `${base}/${latest.issue.number}/comments`,
      { method: "POST", data: { body: pending } },
    );
    if (!Number.isSafeInteger(created?.id))
      throw Error("Missing created comment ID");
    latest.comments.push({
      id: created.id,
      body: pending,
      user: { login: bot, type: "Bot" },
    });
  }
  if (action === "add")
    await api.request(`${base}/${latest.issue.number}/labels`, {
      method: "POST",
      data: { labels: ["needs-info"] },
    });
  if (action === "remove")
    await api.request(`${base}/${latest.issue.number}/labels/needs-info`, {
      method: "DELETE",
    });
  if (action)
    await api.request(`${base}/comments/${ownComment(latest, bot).id}`, {
      method: "PATCH",
      data: { body },
    });
  return "Issue documentation updated";
}

export async function main(command) {
  const api = new GitHub();
  const directory = resolve(".issue-intake");
  const bot = `${process.env.HA_MCP_APP_SLUG}[bot]`;
  if (!/^[a-z0-9-]+\[bot\]$/.test(bot) || bot.startsWith("undefined"))
    throw Error("HA_MCP_APP_SLUG is required");
  if (command === "collect") {
    const event = JSON.parse(
      readFileSync(process.env.GITHUB_EVENT_PATH, "utf8"),
    );
    const number = Number(process.env.ISSUE_NUMBER || event.issue?.number);
    if (event.issue?.pull_request || event.sender?.type === "Bot") return;
    if (process.env.GITHUB_EVENT_NAME === "workflow_dispatch") {
      for (const actor of new Set([
        process.env.GITHUB_ACTOR,
        process.env.GITHUB_TRIGGERING_ACTOR,
      ])) {
        if (!actor) throw Error("Missing dispatch actor");
        const permission = await api.request(
          `repos/${process.env.GITHUB_REPOSITORY}/collaborators/${encodeURIComponent(actor)}/permission`,
        );
        if (!isMaintainer(permission.role_name))
          throw Error("Maintainer dispatch required");
      }
    }
    const snapshot = await collect(api, process.env.GITHUB_REPOSITORY, number);
    const prepared = prepare(snapshot, bot, {
      force: process.env.GITHUB_EVENT_NAME === "workflow_dispatch",
    });
    mkdirSync(directory, { recursive: true });
    if (prepared.run) {
      writeFileSync(
        resolve(directory, "prepared.json"),
        JSON.stringify(prepared),
      );
      writeFileSync(resolve(directory, "prompt.txt"), prompt(prepared.context));
      writeFileSync(resolve(directory, "schema.json"), JSON.stringify(schema));
    }
    appendFileSync(process.env.GITHUB_OUTPUT, `run=${prepared.run}\n`);
    console.log(prepared.run ? "Human context collected" : prepared.reason);
  } else if (command === "publish") {
    if (process.env.TOKEN_APP_SLUG !== process.env.HA_MCP_APP_SLUG)
      throw Error("Installation token belongs to a different App");
    const prepared = JSON.parse(
      readFileSync(resolve(directory, "prepared.json"), "utf8"),
    );
    if (prepared.snapshot.repository !== process.env.GITHUB_REPOSITORY)
      throw Error("Publication repository mismatch");
    const result = JSON.parse(readFileSync(process.env.OUTPUT_PATH, "utf8"));
    console.log(await publish(api, prepared, result, bot));
  } else throw Error("Expected collect or publish");
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
  main(process.argv[2]).catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
