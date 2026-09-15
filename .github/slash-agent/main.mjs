import {
  appendFileSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { decide, principal, resultSchema } from "./core.mjs";
import { API, collect, eventTarget, snapshotGuard } from "./github.mjs";
import { packageWork } from "./worker.mjs";
import { publish } from "./publish.mjs";

const here = dirname(fileURLToPath(import.meta.url));
export function prepare(api, trigger, app) {
  if (!trigger) return null;
  const snapshot = collect(api, trigger.number, app);
  const decision = decide(snapshot, trigger);
  if (decision.mode === "idle") return null;
  return {
    version: 1,
    repository: api.repository,
    app,
    snapshot,
    decision,
    guard: snapshotGuard(snapshot),
  };
}

export function prompt(plan) {
  const { snapshot: s, decision: d } = plan;
  return `${readFileSync(resolve(here, "instructions.md"), "utf8")}\n\nAuthenticated maintainer task:\n${JSON.stringify({ author: principal(d.latest).login, task: d.task })}\n\nSource material:\n${JSON.stringify(
    {
      repository: s.repository,
      issue: { number: s.root, title: s.issue.title, body: s.issue.body },
      pr: s.pr && {
        number: s.pr.number,
        title: s.pr.title,
        body: s.pr.body,
        head: s.head,
      },
      conversation: s.sourceComments,
      feedback: s.feedback,
      threads: s.threads.filter((t) => !t.isResolved),
      checks: s.checks,
      previousMemory: s.session?.summary ?? "",
      round: d.rounds + 1,
    },
  )}`;
}

export function main(operation, env = process.env) {
  const directory = resolve(env.SLASH_STATE_DIR ?? "slash-state");
  const output = (name, value) => {
    if (env.GITHUB_OUTPUT)
      appendFileSync(env.GITHUB_OUTPUT, `${name}=${value}\n`);
  };
  const json = (name) =>
    JSON.parse(readFileSync(resolve(directory, name), "utf8"));
  if (operation === "admit") {
    const app = env.HA_MCP_APP_SLUG;
    if (!/^[a-z0-9-]+$/.test(app ?? ""))
      throw Error("HA_MCP_APP_SLUG is required");
    const api = new API(env.GITHUB_REPOSITORY);
    const event = JSON.parse(readFileSync(env.GITHUB_EVENT_PATH, "utf8"));
    const trigger = eventTarget(api, event, env.GITHUB_EVENT_NAME, env);
    const plan = prepare(api, trigger, app);
    output("run", !!plan);
    if (!plan) return;
    mkdirSync(directory, { recursive: true });
    writeFileSync(resolve(directory, "plan.json"), JSON.stringify(plan));
    output("mode", plan.decision.mode);
    output("head", plan.snapshot.head);
    output("model", plan.decision.parsed.model);
    return;
  }
  const plan = json("plan.json");
  if (operation === "prompt") {
    writeFileSync(resolve(directory, "prompt.txt"), prompt(plan));
    writeFileSync(
      resolve(directory, "schema.json"),
      JSON.stringify(resultSchema),
    );
  } else if (operation === "package") {
    const result = JSON.parse(readFileSync(env.OUTPUT_PATH, "utf8"));
    const artifact = packageWork(
      env.SLASH_SOURCE_DIR ?? "source",
      plan,
      result,
      readFileSync(env.CODEX_LOG_PATH, "utf8"),
    );
    writeFileSync(resolve(directory, "result.json"), JSON.stringify(artifact));
  } else if (operation === "publish") {
    if (env.TOKEN_APP_SLUG !== env.HA_MCP_APP_SLUG)
      throw Error("Unexpected publication App");
    const workerSucceeded = env.WORKER_RESULT === "success";
    const artifact = workerSucceeded ? json("result.json") : null;
    const state = publish(
      new API(env.GITHUB_REPOSITORY),
      plan,
      artifact,
      env.HA_MCP_APP_SLUG,
      { runId: env.GITHUB_RUN_ID, workerSucceeded },
    );
    console.log(
      state.skipped ? "Stale work skipped" : `Slash session: ${state.status}`,
    );
  } else throw Error("Unknown slash controller operation");
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
  try {
    main(process.argv[2]);
  } catch (error) {
    console.error(`::error::${String(error.message).replace(/[\r\n]/g, " ")}`);
    process.exitCode = 1;
  }
}
