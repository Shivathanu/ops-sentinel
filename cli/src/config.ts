import { readFileSync } from "node:fs";
import path from "node:path";

export class ConfigError extends Error {
  constructor(readonly problems: readonly string[]) {
    super(`Invalid configuration:\n  - ${problems.join("\n  - ")}`);
    this.name = "ConfigError";
  }
}

/** The command line was used wrongly; reported with usage help, not as a crash. */
export class UsageError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "UsageError";
  }
}

/** A pre-approved fix. The engine only ever learns its id; the command stays on this machine. */
export interface FixConfig {
  id: string;
  run: string;
  maxAttempts: number;
  timeoutSeconds: number;
}

interface SourceBase {
  name: string;
  cwd: string;
  intervalSeconds: number;
  fix?: FixConfig;
}

export interface CommandSource extends SourceBase {
  type: "command";
  run: string;
  timeoutSeconds: number;
}

export interface FileSource extends SourceBase {
  type: "file";
  path: string;
  pattern: RegExp;
}

export interface HttpSource extends SourceBase {
  type: "http";
  url: string;
  expectStatus: number[];
  timeoutSeconds: number;
}

export type Source = CommandSource | FileSource | HttpSource;

export interface Config {
  baseDir: string;
  engine: { url: string; apiKey: string };
  pollSeconds: number;
  approvalTimeoutMinutes: number;
  logFile: string;
  sources: Source[];
}

export interface ParseOptions {
  env: NodeJS.ProcessEnv;
  baseDir: string;
  /** `sentinel run` wraps one command and needs the engine settings but no declared sources. */
  requireSources: boolean;
}

export const NAME_PATTERN = /^[A-Za-z0-9_.-]{1,100}$/;
export const FIX_ID_PATTERN = /^[A-Za-z0-9_.-]{1,64}$/;
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

type Obj = Record<string, unknown>;

function isObj(value: unknown): value is Obj {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

class Reader {
  constructor(
    private readonly obj: Obj,
    private readonly where: string,
    private readonly problems: string[],
  ) {}

  string(key: string, options: { required?: boolean } = {}): string | undefined {
    const value = this.obj[key];
    if (value === undefined) {
      if (options.required) this.problems.push(`${this.where}.${key} is required`);
      return undefined;
    }
    if (typeof value !== "string" || value.trim() === "") {
      this.problems.push(`${this.where}.${key} must be a non-empty string`);
      return undefined;
    }
    return value;
  }

  int(key: string, fallback: number, min: number, max: number): number {
    const value = this.obj[key];
    if (value === undefined) return fallback;
    if (typeof value !== "number" || !Number.isInteger(value) || value < min || value > max) {
      this.problems.push(`${this.where}.${key} must be a whole number between ${min} and ${max}`);
      return fallback;
    }
    return value;
  }
}

export function loadConfig(file: string, env: NodeJS.ProcessEnv, requireSources = true): Config {
  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(file, "utf8"));
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    throw new ConfigError([`cannot read ${file}: ${reason}`]);
  }
  return parseConfig(raw, { env, baseDir: path.dirname(path.resolve(file)), requireSources });
}

/** Validates everything up front and reports every problem at once, so a bad config fails before any job runs. */
export function parseConfig(raw: unknown, options: ParseOptions): Config {
  const problems: string[] = [];
  if (!isObj(raw)) throw new ConfigError(["the config must be a JSON object"]);

  const engine = parseEngine(raw["engine"], options.env, problems);
  const top = new Reader(raw, "config", problems);
  const pollSeconds = top.int("pollSeconds", 10, 1, 3600);
  const approvalTimeoutMinutes = top.int("approvalTimeoutMinutes", 60, 1, 10_080);
  const logFile = path.resolve(options.baseDir, top.string("logFile") ?? ".sentinel/events.jsonl");

  const sources: Source[] = [];
  const rawSources = raw["sources"] ?? [];
  if (!Array.isArray(rawSources)) {
    problems.push("config.sources must be an array");
  } else {
    const seen = new Set<string>();
    rawSources.forEach((entry, index) => {
      // Checked on the raw name so a duplicate is reported even when the entry has other problems.
      const rawName = isObj(entry) && typeof entry["name"] === "string" ? entry["name"] : undefined;
      if (rawName !== undefined) {
        if (seen.has(rawName)) problems.push(`source name '${rawName}' is used more than once`);
        seen.add(rawName);
      }
      const source = parseSource(entry, index, options.baseDir, problems);
      if (source) sources.push(source);
    });
    if (options.requireSources && rawSources.length === 0) problems.push("config.sources needs at least one source");
  }

  if (problems.length > 0 || !engine) throw new ConfigError(problems);
  return { baseDir: options.baseDir, engine, pollSeconds, approvalTimeoutMinutes, logFile, sources };
}

function parseEngine(
  raw: unknown,
  env: NodeJS.ProcessEnv,
  problems: string[],
): { url: string; apiKey: string } | undefined {
  if (!isObj(raw)) {
    problems.push("config.engine is required");
    return undefined;
  }
  const reader = new Reader(raw, "engine", problems);
  const rawUrl = reader.string("url", { required: true });
  const keyEnv = reader.string("apiKeyEnv") ?? "OPS_SENTINEL_API_KEY";
  const allowInsecure = raw["allowInsecureHttp"] === true;

  let url: string | undefined;
  if (rawUrl !== undefined) {
    try {
      const parsed = new URL(rawUrl);
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
        problems.push("engine.url must start with http:// or https://");
      } else if (parsed.protocol === "http:" && !LOCAL_HOSTS.has(parsed.hostname) && !allowInsecure) {
        // The API key travels in a header on every request, so cleartext is refused off the local machine.
        problems.push(
          `engine.url uses plain http to '${parsed.hostname}'; use https, or set engine.allowInsecureHttp to true to accept the risk`,
        );
      } else {
        url = rawUrl.replace(/\/+$/, "");
      }
    } catch {
      problems.push(`engine.url '${rawUrl}' is not a valid URL`);
    }
  }

  const apiKey = env[keyEnv];
  if (!apiKey) problems.push(`environment variable ${keyEnv} (the engine API key) is not set`);
  return url !== undefined && apiKey ? { url, apiKey } : undefined;
}

function parseSource(entry: unknown, index: number, baseDir: string, problems: string[]): Source | undefined {
  const where = `sources[${index}]`;
  if (!isObj(entry)) {
    problems.push(`${where} must be an object`);
    return undefined;
  }
  const reader = new Reader(entry, where, problems);
  const name = reader.string("name", { required: true });
  if (name !== undefined && !NAME_PATTERN.test(name)) {
    problems.push(`${where}.name may only contain letters, digits, '.', '_' and '-' (max 100)`);
  }
  const type = reader.string("type", { required: true });
  const cwd = path.resolve(baseDir, reader.string("cwd") ?? ".");
  const fix = parseFix(entry["fix"], where, problems);
  const label = name ?? where;

  if (type === "command") {
    const run = reader.string("run", { required: true });
    const intervalSeconds = reader.int("intervalSeconds", 300, 1, 86_400);
    const timeoutSeconds = reader.int("timeoutSeconds", 120, 1, 86_400);
    if (name === undefined || run === undefined) return undefined;
    return { type, name: label, cwd, intervalSeconds, timeoutSeconds, run, ...(fix && { fix }) };
  }
  if (type === "file") {
    const file = reader.string("path", { required: true });
    const match = reader.string("match", { required: true });
    const intervalSeconds = reader.int("intervalSeconds", 5, 1, 86_400);
    let pattern: RegExp | undefined;
    if (match !== undefined) {
      try {
        pattern = new RegExp(match);
      } catch (error) {
        problems.push(`${where}.match is not a valid regular expression: ${(error as Error).message}`);
      }
    }
    if (name === undefined || file === undefined || pattern === undefined) return undefined;
    return { type, name: label, cwd, intervalSeconds, path: path.resolve(baseDir, file), pattern, ...(fix && { fix }) };
  }
  if (type === "http") {
    const url = reader.string("url", { required: true });
    const intervalSeconds = reader.int("intervalSeconds", 60, 1, 86_400);
    const timeoutSeconds = reader.int("timeoutSeconds", 10, 1, 600);
    const expectStatus = parseStatuses(entry["expectStatus"], where, problems);
    if (url !== undefined && !/^https?:\/\//i.test(url)) problems.push(`${where}.url must start with http:// or https://`);
    if (name === undefined || url === undefined) return undefined;
    return { type, name: label, cwd, intervalSeconds, timeoutSeconds, url, expectStatus, ...(fix && { fix }) };
  }
  if (type !== undefined) problems.push(`${where}.type '${type}' is not one of: command, file, http`);
  return undefined;
}

function parseStatuses(raw: unknown, where: string, problems: string[]): number[] {
  if (raw === undefined) return [200];
  const list = Array.isArray(raw) ? raw : [raw];
  const valid = list.every((code) => typeof code === "number" && Number.isInteger(code) && code >= 100 && code <= 599);
  if (!valid || list.length === 0) {
    problems.push(`${where}.expectStatus must be an HTTP status code or a list of them`);
    return [200];
  }
  return list as number[];
}

function parseFix(raw: unknown, where: string, problems: string[]): FixConfig | undefined {
  if (raw === undefined) return undefined;
  if (!isObj(raw)) {
    problems.push(`${where}.fix must be an object`);
    return undefined;
  }
  const reader = new Reader(raw, `${where}.fix`, problems);
  const id = reader.string("id", { required: true });
  const run = reader.string("run", { required: true });
  const maxAttempts = reader.int("maxAttempts", 1, 1, 10);
  const timeoutSeconds = reader.int("timeoutSeconds", 300, 1, 86_400);
  if (id !== undefined && !FIX_ID_PATTERN.test(id)) {
    problems.push(`${where}.fix.id may only contain letters, digits, '.', '_' and '-' (max 64)`);
  }
  return id !== undefined && run !== undefined ? { id, run, maxAttempts, timeoutSeconds } : undefined;
}
