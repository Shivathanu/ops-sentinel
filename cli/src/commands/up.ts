import { existsSync } from "node:fs";
import path from "node:path";
import { Flags } from "@oclif/core";
import { SentinelCommand } from "../base.js";
import { EngineClient } from "../lib/client.js";
import { UsageError } from "../lib/config.js";
import { compose } from "../lib/docker.js";
import { sleep } from "../lib/time.js";

export default class Up extends SentinelCommand {
  static override summary = "Start the engine (and the local model, if configured) with Docker Compose";
  static override flags = {
    config: Flags.string({ char: "c", description: "config file (default: sentinel.config.json in --dir)" }),
    timeout: Flags.integer({ description: "seconds to wait for the engine to become healthy", default: 90 }),
  };

  async run(): Promise<void> {
    const { flags } = await this.parse(Up);
    const root = this.projectDir(flags.dir);
    if (!existsSync(path.join(root, "docker-compose.yml"))) {
      throw new UsageError(`no docker-compose.yml in ${root}; run 'sentinel init' first`);
    }
    const env = this.projectEnv(flags.dir);
    const started = compose(root, ["up", "-d", "--build"]);
    if (started.missing) throw new UsageError("docker was not found; install Docker and make sure 'docker compose' works");
    if (!started.ok) this.error("docker compose up failed; see the output above", { exit: 1 });

    if (env["LLM_PROVIDER"] === "ollama") {
      const model = env["OLLAMA_MODEL"] ?? "llama3.2";
      this.log(`Pulling model ${model} (first run downloads it)...`);
      if (!compose(root, ["exec", "-T", "ollama", "ollama", "pull", model]).ok) {
        this.warn(`could not pull ${model}; run: docker compose exec ollama ollama pull ${model}`);
      }
    }

    const config = this.loadProjectConfig(flags.dir, flags.config, false);
    const client = new EngineClient(config.engine.url, config.engine.apiKey, 3000);
    const deadline = Date.now() + flags.timeout * 1000;
    for (;;) {
      try {
        await client.health();
        break;
      } catch (error) {
        if (Date.now() > deadline) {
          this.error(`engine did not become healthy at ${config.engine.url}: ${(error as Error).message}. Check 'docker compose logs engine'.`, { exit: 1 });
        }
        await sleep(2000);
      }
    }
    this.log(`Engine is up at ${config.engine.url}. Next: sentinel watch`);
  }
}
