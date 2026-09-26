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
  const snapshot = collect(api, trigger.number, app, {
    idleIfUnowned: trigger.automatic,
  });
  if (!snapshot) return null;
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
  const content = `${readFileSync(resolve(here, "instructions.md"), "utf8")}\n\nAuthenticated maintainer task:\n${JSON.stringify({ author: principal(d.latest).login, task: d.task })}\n\nSource material:\n${JSON.stringify(
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
  if (Buffer.byteLength(content, "utf8") > 512000)
    throw Error("Slash prompt exceeds the 512 KiB context limit");
  return content;
}

export function main(
  operation,
  env = process.env,
  {
    createApi = (repository) => new API(repository),
    packageWorkFn = packageWork,
    publishFn = publish,
  } = {},
) {
  const directory = resolve(env.SLASH_STATE_DIR ?? "slash-state");
  const output = (name, value) => {
    if (!env.GITHUB_OUTPUT) throw Error("GITHUB_OUTPUT is required");
    appendFileSync(env.GITHUB_OUTPUT, `${name}=${value}\n`);
  };
  const readJson = (path, label) => {
    try {
      return JSON.parse(readFileSync(path, "utf8"));
    } catch (error) {
      throw Error(`Cannot read valid ${label} for ${operation}: ${error.message}`);
    }
  };
  const json = (name) => readJson(resolve(directory, name), name);
  if (operation === "admit") {
    const app = env.HA_MCP_APP_SLUG;
    if (!app) {
      console.log("::notice::HA_MCP_AGENT_APP_SLUG is unset; slash agent is inactive");
      output("run", false);
      return;
    }
    if (!/^[a-z0-9-]+$/.test(app))
      throw Error("Invalid HA_MCP_APP_SLUG");
    const api = createApi(env.GITHUB_REPOSITORY);
    const event = readJson(env.GITHUB_EVENT_PATH, "GitHub event");
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
    const result = readJson(env.OUTPUT_PATH, "model result");
    const artifact = packageWorkFn(
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
    const state = publishFn(
      createApi(env.GITHUB_REPOSITORY),
      plan,
      artifact,
      env.HA_MCP_APP_SLUG,
      { runId: env.GITHUB_RUN_ID, workerSucceeded },
    );
    if (state.skipped)
      throw Error(`Stale work skipped: ${(state.changed ?? ["unknown guard"]).join(", ")}; send a fresh slash command if needed`);
    console.log(`Slash session: ${state.status}`);
  } else throw Error("Unknown slash controller operation");
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
  try {
    main(process.argv[2]);
  } catch (error) {
    const message = String(error.message).replace(/[\r\n]/g, " ");
    const trace = String(error.stack ?? "")
      .split(/\r?\n/)
      .slice(1, 4)
      .map((line) => line.trim())
      .join(" | ");
    console.error(`::error::${message}${trace ? ` | ${trace}` : ""}`);
    process.exitCode = 1;
  }
}
