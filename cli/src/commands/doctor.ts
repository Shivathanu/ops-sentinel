import { existsSync } from "node:fs";
import path from "node:path";
import { Flags } from "@oclif/core";
import { DEFAULT_CONFIG, SentinelCommand } from "../base.js";
import { EngineClient } from "../lib/client.js";
import { ConfigError } from "../lib/config.js";
import { dockerAvailable } from "../lib/docker.js";

export default class Doctor extends SentinelCommand {
  static override summary = "Check that the project, engine and model are ready";
  static override flags = {
    config: Flags.string({ char: "c", description: "config file (default: sentinel.config.json in --dir)" }),
    offline: Flags.boolean({ description: "skip the engine, Docker and Ollama checks" }),
  };

  async run(): Promise<void> {
    const { flags } = await this.parse(Doctor);
    const root = this.projectDir(flags.dir);
    const env = this.projectEnv(flags.dir);
    let failed = false;
    const ok = (message: string): void => this.log(`  ok    ${message}`);
    const bad = (message: string): void => {
      failed = true;
      this.log(`  FAIL  ${message}`);
    };
    const warn = (message: string): void => this.log(`  warn  ${message}`);

    const major = Number(process.versions.node.split(".")[0]);
    if (major >= 22) ok(`Node ${process.versions.node}`);
    else bad(`Node ${process.versions.node}; this CLI needs 22 or newer`);

    for (const name of ["OPS_SENTINEL_API_KEY"]) {
      if (env[name]) ok(`${name} is set`);
      else bad(`${name} is not set (run 'sentinel init' or add it to .env)`);
    }
    const provider = env["LLM_PROVIDER"] ?? "openai";
    const keyVar: Record<string, string> = { openai: "OPENAI_API_KEY", anthropic: "ANTHROPIC_API_KEY" };
    if (provider === "ollama") ok("LLM_PROVIDER=ollama (no model key needed)");
    else if (keyVar[provider]) {
      if (env[keyVar[provider] as string]) ok(`${keyVar[provider]} is set`);
      else warn(`${keyVar[provider]} is empty; the engine cannot triage until it is set`);
    } else bad(`LLM_PROVIDER '${provider}' is not one of openai, anthropic, ollama`);
    if (!env["SLACK_BOT_TOKEN"]) warn("SLACK_BOT_TOKEN is empty; approvals cannot be posted to Slack");
    if (!env["SLACK_APPROVER_IDS"]) warn("SLACK_APPROVER_IDS is empty; nobody will be able to approve");

    const businessContext = path.join(root, "business-context.md");
    if (existsSync(businessContext)) ok("business-context.md present");
    else warn("business-context.md not found; triage will run without business context");

    let engineUrl: string | undefined;
    let apiKey: string | undefined;
    try {
      const config = this.loadProjectConfig(flags.dir, flags.config);
      ok(`${flags.config ?? DEFAULT_CONFIG}: ${config.sources.length} source(s)`);
      engineUrl = config.engine.url;
      apiKey = config.engine.apiKey;
    } catch (error) {
      if (error instanceof ConfigError) bad(error.message.replaceAll("\n", "\n        "));
      else throw error;
    }

    if (!flags.offline) {
      if (dockerAvailable()) ok("docker compose available");
      else warn("docker compose not found; 'sentinel up' needs it");
      if (provider === "ollama") {
        try {
          const response = await fetch("http://localhost:11434/api/tags", { signal: AbortSignal.timeout(3000) });
          if (response.ok) ok("Ollama reachable on localhost:11434");
          else warn(`Ollama answered ${response.status}`);
        } catch {
          warn("Ollama not reachable on localhost:11434 (fine until 'sentinel up' starts it)");
        }
      }
      if (engineUrl && apiKey) {
        try {
          await new EngineClient(engineUrl, apiKey, 5000).health();
          ok(`engine reachable at ${engineUrl}`);
        } catch (error) {
          bad(`engine not reachable at ${engineUrl}: ${(error as Error).message} (run 'sentinel up')`);
        }
      }
    }
    if (failed) this.exit(1);
    this.log("All required checks passed.");
  }
}
