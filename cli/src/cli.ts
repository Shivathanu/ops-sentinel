import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { parseArgs } from "node:util";
import { EngineClient, EngineError } from "./client.js";
import { ConfigError, loadConfig, parseConfig, UsageError, type Config } from "./config.js";
import { EventLog } from "./log.js";
import { checkOnce, monitorSources, type MonitorContext } from "./monitor.js";
import { sleep } from "./time.js";
import { parseFixFlags, runWrapped } from "./wrap.js";

export interface Io {
  out: (line: string) => void;
  err: (line: string) => void;
  env: NodeJS.ProcessEnv;
  cwd: string;
  version: string;
  /** Overridable so tests can speed up retries; real use leaves the defaults. */
  retryDelayMs?: number;
  fetchImpl?: typeof fetch;
}

const DEFAULT_CONFIG = "sentinel.config.json";
const DEFAULT_LOG = ".sentinel/events.jsonl";

const HELP = `sentinel: watch your automation, report failures, run a pre-approved fix once a person approves it.

Usage:
  sentinel init [--force]                     write a sample ${DEFAULT_CONFIG}
  sentinel check [-c file] [--offline]        validate the config and ping the engine
  sentinel watch [-c file] [--once]           watch every source until stopped (--once: check once, exit 1 on failure)
  sentinel run [options] -- <command...>      run one command; report it if it fails
  sentinel status [--log file] [-n count] [--json]
                                              show recent events from the local log

Options for run:
  --name <name>           source name shown in incidents (default: the command's file name)
  -c, --config <file>     config to take engine settings and a matching source's fix from
  --fix-id <id> --fix-run <command>
                          offer this fix for the failure (both required together)
  --wait                  stay until a person decides and any approved fix has run

Engine settings come from the config file, or for 'run' without one from SENTINEL_ENGINE_URL and
OPS_SENTINEL_API_KEY. The API key is only ever read from the environment.
`;

const SAMPLE_CONFIG = {
  engine: { url: "http://localhost:8000", apiKeyEnv: "OPS_SENTINEL_API_KEY" },
  pollSeconds: 10,
  approvalTimeoutMinutes: 60,
  sources: [
    {
      name: "nightly-export",
      type: "command",
      run: "./export.sh",
      intervalSeconds: 3600,
      timeoutSeconds: 300,
      fix: { id: "rerun-export", run: "./export.sh --retry", maxAttempts: 2, timeoutSeconds: 600 },
    },
    { name: "etl-log", type: "file", path: "/var/log/etl.log", match: "ERROR|FATAL" },
    { name: "crm-api", type: "http", url: "https://example.com/health", intervalSeconds: 60, expectStatus: 200 },
  ],
};

export async function main(argv: string[], io: Io, signal: AbortSignal): Promise<number> {
  const [command, ...rest] = argv;
  try {
    switch (command) {
      case undefined:
      case "help":
      case "-h":
      case "--help":
        io.out(HELP);
        return command === undefined ? 2 : 0;
      case "-v":
      case "--version":
      case "version":
        io.out(io.version);
        return 0;
      case "init":
        return init(rest, io);
      case "check":
        return await check(rest, io);
      case "watch":
        return await watch(rest, io, signal);
      case "run":
        return await run(rest, io, signal);
      case "status":
        return status(rest, io);
      default:
        throw new UsageError(`unknown command '${command}'`);
    }
  } catch (error) {
    if (error instanceof UsageError) {
      io.err(`error: ${error.message}\nRun 'sentinel help' for usage.`);
      return 2;
    }
    if (error instanceof ConfigError || error instanceof EngineError) {
      io.err(`error: ${error.message}`);
      return 2;
    }
    throw error;
  }
}

function parse<T extends Record<string, { type: "string" | "boolean"; short?: string }>>(
  args: string[],
  options: T,
  allowPositionals = false,
) {
  try {
    return parseArgs({ args, options, allowPositionals, strict: true });
  } catch (error) {
    throw new UsageError((error as Error).message);
  }
}

function init(args: string[], io: Io): number {
  const { values } = parse(args, { force: { type: "boolean" } });
  const target = path.resolve(io.cwd, DEFAULT_CONFIG);
  if (existsSync(target) && !values.force) {
    throw new UsageError(`${DEFAULT_CONFIG} already exists; use --force to overwrite it`);
  }
  writeFileSync(target, `${JSON.stringify(SAMPLE_CONFIG, null, 2)}\n`);
  io.out(`Wrote ${DEFAULT_CONFIG}. Edit the sources, then set OPS_SENTINEL_API_KEY and run 'sentinel check'.`);
  return 0;
}

async function check(args: string[], io: Io): Promise<number> {
  const { values } = parse(args, { config: { type: "string", short: "c" }, offline: { type: "boolean" } });
  const config = loadConfig(path.resolve(io.cwd, values.config ?? DEFAULT_CONFIG), io.env);
  io.out(`Config OK. Engine ${config.engine.url}; ${config.sources.length} source(s):`);
  for (const source of config.sources) {
    const fix = source.fix ? `fix '${source.fix.id}' (up to ${source.fix.maxAttempts} attempt(s))` : "no fix";
    io.out(`  ${source.name}  ${source.type}  every ${source.intervalSeconds}s  ${fix}`);
  }
  if (values.offline) return 0;
  try {
    await new EngineClient(config.engine.url, config.engine.apiKey, 10_000, io.fetchImpl).health();
    io.out("Engine reachable.");
    return 0;
  } catch (error) {
    io.err(`error: ${(error as Error).message}`);
    return 1;
  }
}

function context(config: Config, io: Io): MonitorContext {
  return {
    config,
    engine: new EngineClient(config.engine.url, config.engine.apiKey, 10_000, io.fetchImpl),
    log: new EventLog(config.logFile, io.err),
    now: Date.now,
    sleep,
    secrets: [config.engine.apiKey],
    ...(io.fetchImpl && { fetchImpl: io.fetchImpl }),
    retryDelayMs: io.retryDelayMs ?? 2000,
  };
}

async function watch(args: string[], io: Io, signal: AbortSignal): Promise<number> {
  const { values } = parse(args, { config: { type: "string", short: "c" }, once: { type: "boolean" } });
  const config = loadConfig(path.resolve(io.cwd, values.config ?? DEFAULT_CONFIG), io.env);
  const ctx = context(config, io);

  if (values.once) {
    const failing = await checkOnce(ctx);
    io.out(failing.length === 0 ? "All sources healthy." : `Failing: ${failing.join(", ")}`);
    return failing.length === 0 ? 0 : 1;
  }
  await monitorSources(ctx, signal);
  return 0;
}

async function run(args: string[], io: Io, signal: AbortSignal): Promise<number> {
  const { values, positionals } = parse(
    args,
    {
      name: { type: "string" },
      config: { type: "string", short: "c" },
      "fix-id": { type: "string" },
      "fix-run": { type: "string" },
      wait: { type: "boolean" },
    },
    true,
  );
  if (positionals.length === 0) throw new UsageError("nothing to run; put the command after '--'");
  const command = positionals.length === 1 ? (positionals[0] as string) : positionals.map(shellQuote).join(" ");
  const name = values.name ?? defaultName(positionals[0] as string);

  const config = runConfig(values.config, io);
  const configured = config.sources.find((source) => source.name === name);
  const fix = parseFixFlags(values["fix-id"], values["fix-run"]) ?? configured?.fix;
  return runWrapped(context(config, io), { name, command, cwd: io.cwd, wait: values.wait ?? false, ...(fix && { fix }) }, signal);
}

/** `run` works from a config file, or from two environment variables for a one-off wrap in CI. */
function runConfig(configFlag: string | undefined, io: Io): Config {
  const file = path.resolve(io.cwd, configFlag ?? DEFAULT_CONFIG);
  if (configFlag !== undefined || existsSync(file)) return loadConfig(file, io.env, false);
  const url = io.env["SENTINEL_ENGINE_URL"];
  if (!url) {
    throw new UsageError(`no ${DEFAULT_CONFIG} here; pass --config or set SENTINEL_ENGINE_URL and OPS_SENTINEL_API_KEY`);
  }
  return parseConfig({ engine: { url } }, { env: io.env, baseDir: io.cwd, requireSources: false });
}

function status(args: string[], io: Io): number {
  const { values } = parse(args, { log: { type: "string" }, n: { type: "string", short: "n" }, json: { type: "boolean" } });
  const file = path.resolve(io.cwd, values.log ?? DEFAULT_LOG);
  const count = Number(values.n ?? "20");
  if (!Number.isInteger(count) || count < 1) throw new UsageError("-n must be a positive whole number");
  if (!existsSync(file)) {
    io.out(`No events recorded yet (${file} does not exist).`);
    return 0;
  }
  const lines = readFileSync(file, "utf8").split("\n").filter((line) => line.trim() !== "").slice(-count);
  for (const line of lines) {
    if (values.json) {
      io.out(line);
      continue;
    }
    try {
      const e = JSON.parse(line) as { ts: string; source: string; type: string; incidentId?: number; detail?: string };
      const incident = e.incidentId === undefined ? "" : ` #${e.incidentId}`;
      io.out(`${e.ts}  ${e.source}  ${e.type}${incident}${e.detail ? `  ${e.detail.split("\n")[0]}` : ""}`);
    } catch {
      io.out(`(unreadable log line) ${line.slice(0, 120)}`);
    }
  }
  return 0;
}

function defaultName(first: string): string {
  const base = path.basename(first).replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9_.-]/g, "-");
  return base === "" ? "job" : base.slice(0, 100);
}

/** Quotes one argument so a shell passes it through as a single word. */
export function shellQuote(arg: string): string {
  if (/^[A-Za-z0-9_@%+=:,./-]+$/.test(arg)) return arg;
  if (process.platform === "win32") return `"${arg.replace(/"/g, '\\"')}"`;
  return `'${arg.replace(/'/g, `'\\''`)}'`;
}
