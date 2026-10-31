import { randomBytes } from "node:crypto";
import { chmodSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

export const PROVIDERS = ["openai", "anthropic", "ollama"] as const;
export type Provider = (typeof PROVIDERS)[number];
export const LISTENERS = ["command", "file", "http", "webhook"] as const;
export type ListenerKind = (typeof LISTENERS)[number];

export const DEFAULT_MODELS: Record<Provider, string> = {
  openai: "gpt-4o-mini",
  anthropic: "claude-sonnet-4-5",
  ollama: "llama3.2",
};

export interface ScaffoldOptions {
  provider: Provider;
  model: string;
  listeners: readonly ListenerKind[];
  /** Image the compose file runs. Build it from the repository with `docker build -t ops-sentinel engine`. */
  engineImage: string;
  /** Path to the engine source tree; when set, compose builds the image from it instead of pulling `engineImage`. */
  engineBuild?: string;
  slackChannel: string;
  slackApprovers: string;
  /** Overridable so tests are deterministic. */
  generateSecret?: () => string;
}

export interface ScaffoldFile {
  path: string;
  content: string;
  /** Files holding secrets are written readable by the owner only. */
  mode?: number;
}

export function randomSecret(): string {
  return randomBytes(32).toString("base64url");
}

export function buildConfig(options: ScaffoldOptions): object {
  const sources: object[] = [];
  const has = (kind: ListenerKind): boolean => options.listeners.includes(kind);
  if (has("command")) {
    sources.push({
      name: "nightly-export",
      type: "command",
      run: "./export.sh",
      intervalSeconds: 3600,
      timeoutSeconds: 300,
      fix: { id: "rerun-export", run: "./export.sh --retry", maxAttempts: 2, timeoutSeconds: 600 },
    });
  }
  if (has("file")) sources.push({ name: "etl-log", type: "file", path: "./logs/etl.log", match: "ERROR|FATAL" });
  if (has("http")) {
    sources.push({ name: "crm-api", type: "http", url: "https://example.com/health", intervalSeconds: 60, expectStatus: 200 });
  }
  if (has("webhook")) {
    sources.push({ name: "alerts-webhook", type: "webhook", host: "127.0.0.1", port: 8787, path: "/event" });
  }
  return {
    engine: { url: "http://localhost:8000", apiKeyEnv: "OPS_SENTINEL_API_KEY" },
    pollSeconds: 10,
    approvalTimeoutMinutes: 60,
    sources,
  };
}

export function buildEnv(options: ScaffoldOptions): string {
  const secret = options.generateSecret ?? randomSecret;
  const lines = [
    "# Created by 'sentinel init'. This file holds secrets: keep it out of version control.",
    "",
    "# Shared by the CLI and the engine. Generated for you.",
    `OPS_SENTINEL_API_KEY=${secret()}`,
  ];
  if (options.listeners.includes("webhook")) {
    lines.push("# Senders of the webhook listener present this as X-Sentinel-Secret or a Bearer token.");
    lines.push(`SENTINEL_WEBHOOK_SECRET=${secret()}`);
  }
  lines.push(
    "",
    "# Engine: incident triage model",
    `LLM_PROVIDER=${options.provider}`,
  );
  if (options.provider === "openai") lines.push("OPENAI_API_KEY=", `OPENAI_MODEL=${options.model}`);
  if (options.provider === "anthropic") lines.push("ANTHROPIC_API_KEY=", `ANTHROPIC_MODEL=${options.model}`);
  if (options.provider === "ollama") {
    lines.push(`OLLAMA_MODEL=${options.model}`, "OLLAMA_BASE_URL=http://ollama:11434/v1", "COMPOSE_PROFILES=ollama");
  }
  lines.push(
    "BUSINESS_CONTEXT_FILE=/app/context/business-context.md",
    "",
    "# Engine: Slack approvals. Only the listed user ids can approve or reject.",
    "SLACK_BOT_TOKEN=",
    "SLACK_SIGNING_SECRET=",
    `SLACK_CHANNEL_ID=${options.slackChannel}`,
    `SLACK_APPROVER_IDS=${options.slackApprovers}`,
    "",
    "# Engine: Exasol (incident memory and audit trail). The defaults reach an Exasol on this machine.",
    "EXASOL_ADMIN_HOST=host.docker.internal",
    "EXASOL_ADMIN_PORT=8563",
    "EXASOL_ADMIN_USER=sentinel_engine",
    "EXASOL_ADMIN_PASSWORD=",
    "EXASOL_EXECUTOR_USER=sentinel_executor",
    "EXASOL_EXECUTOR_PASSWORD=",
    "EXASOL_SCHEMA=SENTINEL",
    "",
    "# This project only reports incidents from the CLI; set true to also poll Exasol pipeline tables.",
    "OPS_SENTINEL_WATCHER_ENABLED=false",
    "",
  );
  return lines.join("\n");
}

export function buildCompose(options: ScaffoldOptions): string {
  const engine = options.engineBuild
    ? `    build: ${JSON.stringify(options.engineBuild)}\n    image: ${options.engineImage}`
    : `    image: ${options.engineImage}`;
  return `# Created by 'sentinel init'. Start with 'sentinel up'.
services:
  engine:
${engine}
    env_file: .env
    ports:
      - "127.0.0.1:8000:8000"
    volumes:
      - ./business-context.md:/app/context/business-context.md:ro
    extra_hosts:
      - "host.docker.internal:host-gateway"
    restart: unless-stopped

  # Local small model. Only starts when COMPOSE_PROFILES=ollama (set in .env when you pick the ollama provider).
  ollama:
    image: ollama/ollama:latest
    profiles: ["ollama"]
    volumes:
      - ollama-models:/root/.ollama
    ports:
      - "127.0.0.1:11434:11434"
    restart: unless-stopped

volumes:
  ollama-models:
`;
}

export const BUSINESS_CONTEXT = `# Business context

The triage model reads this file with every incident, so it can describe impact in your terms.
Replace the examples. Keep it short (the engine reads the first 8000 characters).

## Critical processes
- Nightly export: feeds finance reporting; must finish before 06:00.

## Owners
- Data platform team: owns ETL jobs and the export. Escalate to #data-platform.

## Never touch
- Production billing tables. Fixes must never write to them.

## Vocabulary
- "Close" means the month-end close, the busiest period of the year.
`;

export function buildReadme(options: ScaffoldOptions): string {
  return `# Ops Sentinel project

Generated by \`sentinel init\`.

- \`sentinel.config.json\`: the sources (listeners) to watch and the pre-approved fix for each.
- \`.env\`: secrets and engine settings. Fill in the blanks (${options.provider === "ollama" ? "Slack and Exasol" : "model key, Slack and Exasol"}).
- \`business-context.md\`: what the triage model should know about your business.
- \`docker-compose.yml\`: the engine${options.provider === "ollama" ? " and a local Ollama model" : ""}.

Next:

1. Edit \`.env\` and \`sentinel.config.json\`.
2. \`sentinel doctor\` checks the setup.
3. \`sentinel up\` starts the engine.
4. \`sentinel watch\` starts the listeners.
`;
}

export function buildProject(options: ScaffoldOptions): ScaffoldFile[] {
  return [
    { path: "sentinel.config.json", content: `${JSON.stringify(buildConfig(options), null, 2)}\n` },
    { path: ".env", content: buildEnv(options), mode: 0o600 },
    { path: "docker-compose.yml", content: buildCompose(options) },
    { path: "business-context.md", content: BUSINESS_CONTEXT },
    { path: ".gitignore", content: ".env\n.sentinel/\n" },
    { path: "README.md", content: buildReadme(options) },
  ];
}

/** Files from `files` that already exist under `dir`. */
export function conflicts(dir: string, files: readonly ScaffoldFile[]): string[] {
  return files.filter((file) => existsSync(path.join(dir, file.path))).map((file) => file.path);
}

export function writeProject(dir: string, files: readonly ScaffoldFile[]): void {
  mkdirSync(dir, { recursive: true });
  for (const file of files) {
    const target = path.join(dir, file.path);
    writeFileSync(target, file.content, { mode: file.mode ?? 0o644 });
    // writeFileSync ignores the mode when overwriting an existing file.
    if (file.mode !== undefined) chmodSync(target, file.mode);
  }
}
