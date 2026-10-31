import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";
import { API_KEY, FakeEngineServer, nodeCommand, tempDir } from "./helpers.js";

const BIN = fileURLToPath(new URL("../../bin/run.js", import.meta.url));
const server = new FakeEngineServer();
before(() => server.start());
after(() => server.stop());

interface Result {
  code: number | null;
  stdout: string;
  stderr: string;
}

function start(args: string[], cwd: string, env: NodeJS.ProcessEnv = { OPS_SENTINEL_API_KEY: API_KEY }) {
  const child = spawn(process.execPath, [BIN, ...args], { cwd, env: { PATH: process.env["PATH"], ...env } });
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (d: Buffer) => (stdout += d.toString()));
  child.stderr.on("data", (d: Buffer) => (stderr += d.toString()));
  const done = new Promise<Result>((resolve) => child.on("close", (code) => resolve({ code, stdout, stderr })));
  return { child, done, output: () => stdout + stderr };
}

const sentinel = (args: string[], cwd: string, env?: NodeJS.ProcessEnv): Promise<Result> => start(args, cwd, env).done;

function writeConfig(dir: string, sources: unknown[], extra: Record<string, unknown> = {}): void {
  writeFileSync(
    path.join(dir, "sentinel.config.json"),
    JSON.stringify({ engine: { url: server.url }, pollSeconds: 1, approvalTimeoutMinutes: 5, sources, ...extra }),
  );
}

async function waitFor(condition: () => boolean, what: string, timeoutMs = 20_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!condition()) {
    if (Date.now() > deadline) assert.fail(`timed out waiting for ${what}`);
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

test("help lists the commands and --version prints the package version", async () => {
  const help = await sentinel(["--help"], tempDir());
  assert.equal(help.code, 0);
  for (const name of ["init", "up", "down", "doctor", "watch", "run", "status"]) assert.match(help.stdout, new RegExp(name));
  const version = await sentinel(["--version"], tempDir());
  assert.match(version.stdout, /ops-sentinel-cli\/\d+\.\d+\.\d+/);
});

test("init scaffolds a project that validates, keeps .env private and refuses to overwrite", async () => {
  const dir = tempDir();
  const first = await sentinel(["init", "--yes", "--provider", "ollama", "--listener", "command", "--listener", "webhook"], dir, {});
  assert.equal(first.code, 0, first.stderr);
  for (const file of ["sentinel.config.json", ".env", "docker-compose.yml", "business-context.md", ".gitignore", "README.md"]) {
    assert.ok(existsSync(path.join(dir, file)), file);
  }
  if (process.platform !== "win32") assert.equal(statSync(path.join(dir, ".env")).mode & 0o077, 0, ".env must be owner-only");
  const env = readFileSync(path.join(dir, ".env"), "utf8");
  assert.match(env, /LLM_PROVIDER=ollama/);
  assert.match(env, /COMPOSE_PROFILES=ollama/);
  assert.match(env, /OPS_SENTINEL_API_KEY=\S{20,}/);

  const doctor = await sentinel(["doctor", "--offline"], dir, {});
  assert.equal(doctor.code, 0, doctor.stdout + doctor.stderr);
  assert.match(doctor.stdout, /2 source\(s\)/);

  const again = await sentinel(["init", "--yes"], dir, {});
  assert.equal(again.code, 2);
  assert.match(again.stderr, /already exist/);
  assert.equal((await sentinel(["init", "--yes", "--force"], dir, {})).code, 0);
});

test("doctor fails when the API key is missing and when the engine is unreachable", async () => {
  const dir = tempDir();
  writeConfig(dir, [{ name: "job", type: "command", run: "true" }]);
  const keyless = await sentinel(["doctor", "--offline"], dir, {});
  assert.equal(keyless.code, 1);
  assert.match(keyless.stdout, /OPS_SENTINEL_API_KEY/);

  assert.equal((await sentinel(["doctor"], dir)).code, 0);
  writeConfig(dir, [{ name: "job", type: "command", run: "true" }], { engine: { url: "http://127.0.0.1:1" } });
  const down = await sentinel(["doctor"], dir);
  assert.equal(down.code, 1);
  assert.match(down.stdout, /engine not reachable/);
});

test("commands read the project .env, but the real environment wins", async () => {
  const dir = tempDir();
  writeConfig(dir, [{ name: "job", type: "command", run: "true" }]);
  writeFileSync(path.join(dir, ".env"), `OPS_SENTINEL_API_KEY=${API_KEY}\n`);
  assert.equal((await sentinel(["doctor"], dir, {})).code, 0);
  assert.equal((await sentinel(["doctor", "--dir", dir], tempDir(), {})).code, 0);
});

test("run passes a successful command through and reports nothing", async () => {
  const dir = tempDir();
  writeConfig(dir, []);
  const before = server.incidents.size;
  const result = await sentinel(["run", "--name", "ok-job", "--", nodeCommand(dir, "ok", "")], dir);
  assert.equal(result.code, 0);
  assert.equal(server.incidents.size, before);
});

test("run reports a failing command, keeps its exit code and does not wait by default", async () => {
  const dir = tempDir();
  writeConfig(dir, []);
  const result = await sentinel(["run", "--name", "bad-job", "--", nodeCommand(dir, "bad", "process.stderr.write('disk full\\n'); process.exit(7)")], dir);
  assert.equal(result.code, 7);
  const incident = [...server.incidents.values()].at(-1);
  assert.equal(incident?.report.source, "bad-job");
  assert.equal(incident?.report.exit_code, 7);
  assert.match(incident?.report.error ?? "", /disk full/);
  assert.match(result.stderr, /reported incident #\d+/);
});

test("run --wait runs the approved fix and exits 0 when it succeeds", async () => {
  const dir = tempDir();
  const marker = path.join(dir, "fixed");
  writeConfig(dir, []);
  const job = nodeCommand(dir, "job", `import { existsSync } from "node:fs"; process.exit(existsSync(${JSON.stringify(marker)}) ? 0 : 5)`);
  const fix = nodeCommand(dir, "fix", `import { writeFileSync } from "node:fs"; writeFileSync(${JSON.stringify(marker)}, "x")`);
  const approver = setInterval(() => {
    for (const [id, incident] of server.incidents) {
      if (incident.report.source === "waiter" && incident.status === "AWAITING_APPROVAL") server.setStatus(id, "APPROVED");
    }
  }, 100);
  try {
    const result = await sentinel(["run", "--name", "waiter", "--fix-id", "make-fixed", "--fix-run", fix, "--wait", "--", job], dir);
    assert.equal(result.code, 0, result.stderr);
    assert.ok(existsSync(marker));
    const incident = [...server.incidents.values()].find((i) => i.report.source === "waiter");
    assert.equal(incident?.status, "REMEDIATED");
    assert.equal(incident?.results[0]?.outcome, "SUCCESS");
  } finally {
    clearInterval(approver);
  }
});

test("run --wait keeps the original exit code when the fix is rejected", async () => {
  const dir = tempDir();
  writeConfig(dir, []);
  const rejecter = setInterval(() => {
    for (const [id, incident] of server.incidents) {
      if (incident.report.source === "rejected-job" && incident.status === "AWAITING_APPROVAL") server.setStatus(id, "REJECTED");
    }
  }, 100);
  try {
    const result = await sentinel(
      ["run", "--name", "rejected-job", "--fix-id", "f", "--fix-run", "exit 0", "--wait", "--", nodeCommand(dir, "j", "process.exit(4)")],
      dir,
    );
    assert.equal(result.code, 4);
  } finally {
    clearInterval(rejecter);
  }
});

test("run keeps the command's exit code even when the engine is unreachable", async () => {
  const dir = tempDir();
  writeConfig(dir, [], { engine: { url: "http://127.0.0.1:1" } });
  const result = await sentinel(["run", "--name", "j", "--", nodeCommand(dir, "j", "process.exit(9)")], dir);
  assert.equal(result.code, 9);
  assert.match(result.stderr, /could not report/);
});

test("run takes engine settings from the environment when there is no config file", async () => {
  const dir = tempDir();
  const before = server.incidents.size;
  const result = await sentinel(["run", "--name", "env-job", "--", nodeCommand(dir, "j", "process.exit(3)")], dir, {
    OPS_SENTINEL_API_KEY: API_KEY,
    SENTINEL_ENGINE_URL: server.url,
  });
  assert.equal(result.code, 3);
  assert.equal(server.incidents.size, before + 1);
});

test("run usage errors exit 2", async () => {
  const dir = tempDir();
  writeConfig(dir, []);
  assert.equal((await sentinel(["run"], dir)).code, 2);
  const noEngine = await sentinel(["run", "--", "true"], tempDir());
  assert.equal(noEngine.code, 2);
  assert.match(noEngine.stderr, /SENTINEL_ENGINE_URL/);
});

test("watch --once exits 1 and reports when a source is failing, 0 when all are healthy", async () => {
  const dir = tempDir();
  writeConfig(dir, [{ name: "broken", type: "command", run: nodeCommand(dir, "b", "process.exit(1)"), fix: { id: "f", run: "true" } }]);
  const failing = await sentinel(["watch", "--once"], dir);
  assert.equal(failing.code, 1);
  assert.match(failing.stdout, /Failing: broken/);

  const shown = await sentinel(["status"], dir);
  assert.match(shown.stdout, /broken\s+reported #\d+/);
  const json = await sentinel(["status", "--json", "-n", "1"], dir);
  assert.equal((JSON.parse(json.stdout.split("\n")[0] ?? "{}") as { type: string }).type, "reported");
  assert.equal((await sentinel(["status", "-n", "0"], dir)).code, 2);

  writeConfig(dir, [{ name: "fine", type: "command", run: nodeCommand(dir, "f", "") }]);
  assert.equal((await sentinel(["watch", "--once"], dir)).code, 0);
});

test("watch runs the whole loop: detect, wait for approval, run the fix, recover, stop on SIGINT", async () => {
  const dir = tempDir();
  const healed = path.join(dir, "healed");
  const job = nodeCommand(dir, "job", `import { existsSync } from "node:fs"; if (!existsSync(${JSON.stringify(healed)})) { console.error("lock held"); process.exit(2); }`);
  const fix = nodeCommand(dir, "fix", `import { writeFileSync } from "node:fs"; writeFileSync(${JSON.stringify(healed)}, "x")`);
  writeConfig(dir, [{ name: "loop-job", type: "command", run: job, intervalSeconds: 1, fix: { id: "unlock", run: fix } }]);
  const running = start(["watch"], dir);

  const find = () => [...server.incidents.entries()].find(([, i]) => i.report.source === "loop-job");
  await waitFor(() => find() !== undefined, "the failure to be reported");
  const [id] = find() ?? [];
  assert.ok(id !== undefined);
  assert.ok(!existsSync(healed), "the fix must not run before approval");

  server.setStatus(id, "APPROVED");
  await waitFor(() => server.incidents.get(id)?.status === "REMEDIATED", "the fix result to be recorded");
  await waitFor(() => /recovered/.test(running.output()), "the source to recover");

  running.child.kill("SIGINT");
  assert.equal((await running.done).code, 0);
  assert.ok(existsSync(healed));
  assert.equal([...server.incidents.values()].filter((i) => i.report.source === "loop-job").length, 1, "must report once");
});

test("watch serves a webhook source: bad secret is refused, a good alert becomes one incident", async () => {
  const dir = tempDir();
  const port = 20_000 + Math.floor(Math.random() * 20_000);
  const secret = "s".repeat(24);
  writeConfig(dir, [{ name: "hook", type: "webhook", port, path: "/event" }]);
  const running = start(["watch"], dir, { OPS_SENTINEL_API_KEY: API_KEY, SENTINEL_WEBHOOK_SECRET: secret });
  try {
    await waitFor(() => /listening on/.test(running.output()), "the webhook to listen");
    const url = `http://127.0.0.1:${port}/event`;
    const bad = await fetch(url, { method: "POST", headers: { "X-Sentinel-Secret": "wrong" }, body: "{}" });
    assert.equal(bad.status, 401);
    const before = server.incidents.size;
    const post = () => fetch(url, { method: "POST", headers: { Authorization: `Bearer ${secret}` }, body: JSON.stringify({ id: "a1", message: "deploy failed" }) });
    assert.equal((await post()).status, 202);
    assert.equal((await post()).status, 202);
    assert.equal(server.incidents.size, before + 1, "a repeated key must not open a second incident");
    assert.match([...server.incidents.values()].at(-1)?.report.error ?? "", /deploy failed/);
  } finally {
    running.child.kill("SIGINT");
    await running.done;
  }
});
