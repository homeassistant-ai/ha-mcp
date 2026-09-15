import { execFileSync } from "node:child_process";
import { lstatSync, readFileSync, realpathSync } from "node:fs";
import { resolve, sep } from "node:path";
import { safePath, validateChanges, validateResult } from "./core.mjs";

export function packageWork(directory, plan, result, log = "") {
  validateResult(result, plan.snapshot);
  const root = realpathSync(directory);
  const git = (...args) =>
    execFileSync(
      "git",
      ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", ...args],
      {
        cwd: root,
        encoding: "utf8",
        maxBuffer: 4 * 1024 * 1024,
        timeout: 30000,
        env: {
          ...process.env,
          GIT_CONFIG_NOSYSTEM: "1",
          GIT_CONFIG_GLOBAL: "/dev/null",
        },
      },
    );
  if (git("rev-parse", "HEAD").trim() !== plan.snapshot.head)
    throw Error("Worker changed Git HEAD");
  const names = [
    ...new Set(
      git(
        "ls-files",
        "--modified",
        "--deleted",
        "--others",
        "--exclude-standard",
        "-z",
      )
        .split("\0")
        .filter(Boolean),
    ),
  ];
  const changes = names.map((name) => {
    safePath(name);
    const parts = name.split("/");
    for (let i = 1; i <= parts.length; i++) {
      const path = resolve(root, ...parts.slice(0, i));
      try {
        if (
          lstatSync(path).isSymbolicLink() ||
          !realpathSync(path).startsWith(root + sep)
        )
          throw Error("Symlink in patch path");
      } catch (error) {
        if (error.code !== "ENOENT") throw error;
      }
    }
    const path = resolve(root, name);
    let stat;
    try {
      stat = lstatSync(path);
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    if (!stat) return { path: name, mode: "100644", content: null };
    if (!stat.isFile() || stat.size > 2 * 1024 * 1024)
      throw Error("Unsupported patch file");
    return {
      path: name,
      mode: stat.mode & 0o111 ? "100755" : "100644",
      content: readFileSync(path).toString("base64"),
    };
  });
  validateChanges(changes);
  if (result.outcome !== "changed" && changes.length)
    throw Error("Model outcome disagrees with changed files");
  if (result.outcome === "changed" && !changes.length)
    throw Error("Model reported changes but produced none");
  let threadId = null;
  for (const line of log.split("\n")) {
    try {
      const event = JSON.parse(line);
      if (
        event.type === "thread.started" &&
        /^[a-f0-9-]{36}$/.test(event.thread_id)
      ) {
        threadId = event.thread_id;
        break;
      }
    } catch {
      /* The CLI may interleave non-JSON diagnostics. */
    }
  }
  if (!threadId)
    throw Error("Codex session ID is missing from the execution log");
  return { result, changes, threadId };
}
