import assert from "node:assert/strict";
import { test } from "node:test";
import { ConfigError, parseConfig } from "../src/lib/config.js";

const env = { OPS_SENTINEL_API_KEY: "key-123456" };
const base = { engine: { url: "http://localhost:8000/" } };
const parse = (raw: unknown, requireSources = true) => parseConfig(raw, { env, baseDir: "/work", requireSources });

function problems(raw: unknown, requireSources = true): string {
  try {
    parse(raw, requireSources);
  } catch (error) {
    assert.ok(error instanceof ConfigError);
    return error.problems.join("\n");
  }
  assert.fail("expected the config to be rejected");
}

test("parses all three source types with defaults and resolves paths against the config directory", () => {
  const config = parse({
    ...base,
    sources: [
      { name: "job", type: "command", run: "./x.sh", fix: { id: "retry", run: "./x.sh --retry" } },
      { name: "log", type: "file", path: "logs/etl.log", match: "ERROR" },
      { name: "api", type: "http", url: "https://example.com/health", expectStatus: [200, 204] },
    ],
  });

  assert.equal(config.engine.url, "http://localhost:8000");
  assert.equal(config.pollSeconds, 10);
  assert.equal(config.sources.length, 3);
  const [job, log, api] = config.sources;
  assert.equal(job?.type === "command" && job.timeoutSeconds, 120);
  assert.equal(job?.fix?.maxAttempts, 1);
  assert.equal(job?.cwd, "/work");
  assert.equal(log?.type === "file" && log.path, "/work/logs/etl.log");
  assert.deepEqual(api?.type === "http" && api.expectStatus, [200, 204]);
  assert.equal(config.logFile, "/work/.sentinel/events.jsonl");
});

test("reports every problem at once", () => {
  const text = problems({
    engine: { url: "ftp://x" },
    pollSeconds: 0,
    sources: [{ name: "bad name", type: "command" }, { name: "a", type: "nope" }, { type: "file" }],
  });
  for (const expected of ["engine.url", "pollSeconds", "sources[0].name", "sources[0].run", "sources[1].type", "sources[2].name"]) {
    assert.ok(text.includes(expected), `missing '${expected}' in:\n${text}`);
  }
});

test("the API key must come from the environment", () => {
  assert.throws(() => parseConfig({ ...base, sources: [] }, { env: {}, baseDir: "/w", requireSources: false }), /OPS_SENTINEL_API_KEY/);
  assert.equal(parseConfig({ ...base, engine: { url: "http://localhost:1", apiKeyEnv: "MY_KEY" } }, { env: { MY_KEY: "k" }, baseDir: "/w", requireSources: false }).engine.apiKey, "k");
});

test("plain http is refused off the local machine unless explicitly allowed", () => {
  assert.match(problems({ engine: { url: "http://sentinel.corp.example" }, sources: [] }, false), /plain http/);
  assert.doesNotThrow(() => parse({ engine: { url: "https://sentinel.corp.example" }, sources: [] }, false));
  assert.doesNotThrow(() => parse({ engine: { url: "http://sentinel.corp.example", allowInsecureHttp: true }, sources: [] }, false));
  assert.doesNotThrow(() => parse({ engine: { url: "http://127.0.0.1:8000" }, sources: [] }, false));
});

test("duplicate names, bad fix ids, bad regexes and bad status codes are rejected", () => {
  const text = problems({
    ...base,
    sources: [
      { name: "a", type: "command", run: "x", fix: { id: "rm -rf /", run: "x" } },
      { name: "a", type: "file", path: "p", match: "(" },
      { name: "b", type: "http", url: "https://x", expectStatus: [42] },
    ],
  });
  assert.match(text, /used more than once/);
  assert.match(text, /fix\.id may only contain/);
  assert.match(text, /not a valid regular expression/);
  assert.match(text, /expectStatus/);
});

test("run mode accepts a config with no sources, watch mode does not", () => {
  assert.equal(parse({ ...base, sources: [] }, false).sources.length, 0);
  assert.match(problems({ ...base, sources: [] }, true), /at least one source/);
});
