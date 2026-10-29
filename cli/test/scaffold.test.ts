import assert from "node:assert/strict";
import { test } from "node:test";
import { parseDotenv, withDotenv } from "../src/lib/dotenv.js";
import { shellQuote } from "../src/lib/shell.js";
import { parseConfig } from "../src/lib/config.js";
import { buildProject, DEFAULT_MODELS, type ScaffoldOptions } from "../src/scaffold/index.js";
import { tempDir } from "./helpers.js";
import { writeFileSync } from "node:fs";
import path from "node:path";

const base: ScaffoldOptions = {
  provider: "openai",
  model: DEFAULT_MODELS.openai,
  listeners: ["command", "file", "http", "webhook"],
  engineImage: "ops-sentinel:latest",
  slackChannel: "C1",
  slackApprovers: "U1",
  generateSecret: (() => {
    let n = 0;
    return () => `secret-number-${n++}-padding`;
  })(),
};

test("scaffolded config passes the real validator for every listener", () => {
  const files = buildProject(base);
  const config = JSON.parse(files.find((f) => f.path === "sentinel.config.json")?.content ?? "");
  const env = parseDotenv(files.find((f) => f.path === ".env")?.content ?? "");
  const parsed = parseConfig(config, { env, baseDir: tempDir(), requireSources: true });
  assert.deepEqual(parsed.sources.map((s) => s.type), ["command", "file", "http", "webhook"]);
});

test("compose builds from source when asked, and only the ollama provider pulls in the model service profile", () => {
  const build = buildProject({ ...base, engineBuild: "../ops-sentinel" });
  assert.match(build.find((f) => f.path === "docker-compose.yml")?.content ?? "", /build: "..\/ops-sentinel"/);
  const env = build.find((f) => f.path === ".env")?.content ?? "";
  assert.doesNotMatch(env, /COMPOSE_PROFILES/);
  assert.equal(build.find((f) => f.path === ".env")?.mode, 0o600);
  assert.match(build.find((f) => f.path === ".gitignore")?.content ?? "", /^\.env$/m);
});

test("parseDotenv handles quotes, comments and export; withDotenv lets the real environment win", () => {
  assert.deepEqual(parseDotenv(`# c\nA=1\nexport B="two words"\nC='x'\n\nbad line\nD=\n`), { A: "1", B: "two words", C: "x", D: "" });
  const file = path.join(tempDir(), ".env");
  writeFileSync(file, "A=file\nB=file\n");
  const merged = withDotenv(file, { A: "env" });
  assert.equal(merged["A"], "env");
  assert.equal(merged["B"], "file");
});

test("shellQuote leaves safe words alone and quotes everything else as one word", () => {
  assert.equal(shellQuote("./run.sh"), "./run.sh");
  if (process.platform !== "win32") {
    assert.equal(shellQuote("a b"), "'a b'");
    assert.equal(shellQuote("it's"), "'it'\\''s'");
    assert.equal(shellQuote("$(rm -rf /)"), "'$(rm -rf /)'");
  }
});
