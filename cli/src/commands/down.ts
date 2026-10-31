import { existsSync } from "node:fs";
import path from "node:path";
import { SentinelCommand } from "../base.js";
import { UsageError } from "../lib/config.js";
import { compose } from "../lib/docker.js";

export default class Down extends SentinelCommand {
  static override summary = "Stop the engine and local model";

  async run(): Promise<void> {
    const { flags } = await this.parse(Down);
    const root = this.projectDir(flags.dir);
    if (!existsSync(path.join(root, "docker-compose.yml"))) throw new UsageError(`no docker-compose.yml in ${root}`);
    const result = compose(root, ["down"]);
    if (result.missing) throw new UsageError("docker was not found");
    if (!result.ok) this.error("docker compose down failed; see the output above", { exit: 1 });
  }
}
