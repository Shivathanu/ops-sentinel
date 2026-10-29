import path from "node:path";
import { Command, Flags } from "@oclif/core";
import { EngineClient, EngineError } from "./lib/client.js";
import { ConfigError, loadConfig, UsageError, type Config } from "./lib/config.js";
import { withDotenv } from "./lib/dotenv.js";
import { EventLog } from "./lib/log.js";
import type { MonitorContext } from "./lib/monitor.js";
import { sleep } from "./lib/time.js";

export const DEFAULT_CONFIG = "sentinel.config.json";
export const DEFAULT_LOG = ".sentinel/events.jsonl";

/** Shared behaviour: a project directory whose `.env` supplies settings, and friendly errors for bad input. */
export abstract class SentinelCommand extends Command {
  static override baseFlags = {
    dir: Flags.string({ char: "d", description: "project directory (holds sentinel.config.json and .env)", default: "." }),
  };

  protected projectDir(dir: string): string {
    return path.resolve(dir);
  }

  /** The process environment with the project's `.env` underneath it. */
  protected projectEnv(dir: string): NodeJS.ProcessEnv {
    return withDotenv(path.join(this.projectDir(dir), ".env"), process.env);
  }

  protected loadProjectConfig(dir: string, configFlag: string | undefined, requireSources = true): Config {
    const root = this.projectDir(dir);
    return loadConfig(path.resolve(root, configFlag ?? DEFAULT_CONFIG), this.projectEnv(dir), requireSources);
  }

  protected monitorContext(config: Config): MonitorContext {
    return {
      config,
      engine: new EngineClient(config.engine.url, config.engine.apiKey),
      log: new EventLog(config.logFile, (line) => process.stderr.write(`${line}\n`)),
      now: Date.now,
      sleep,
      secrets: [config.engine.apiKey],
      retryDelayMs: 2000,
    };
  }

  /** An AbortSignal that fires on the first Ctrl-C or SIGTERM; a second one force-quits. */
  protected shutdownSignal(): AbortSignal {
    const controller = new AbortController();
    let interrupted = false;
    for (const name of ["SIGINT", "SIGTERM"] as const) {
      process.on(name, () => {
        if (interrupted) process.exit(130);
        interrupted = true;
        process.stderr.write(`\nReceived ${name}; stopping. Send it again to force quit.\n`);
        controller.abort();
      });
    }
    return controller.signal;
  }

  protected override async catch(error: Error & { exitCode?: number }): Promise<unknown> {
    if (error instanceof UsageError || error instanceof ConfigError || error instanceof EngineError) {
      this.error(error.message, { exit: 2 });
    }
    return super.catch(error);
  }
}
