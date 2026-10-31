import { Flags } from "@oclif/core";
import { SentinelCommand } from "../base.js";
import { checkOnce, monitorSources } from "../lib/monitor.js";

export default class Watch extends SentinelCommand {
  static override summary = "Run every listener in the config until stopped";
  static override description =
    "Starts the configured sources (commands, log files, HTTP checks, webhook receivers), reports failures to the engine, and runs a pre-approved fix once a person approves it.";
  static override flags = {
    config: Flags.string({ char: "c", description: "config file (default: sentinel.config.json in --dir)" }),
    once: Flags.boolean({ description: "check every source once and exit 1 if any is failing" }),
  };

  async run(): Promise<void> {
    const { flags } = await this.parse(Watch);
    const config = this.loadProjectConfig(flags.dir, flags.config);
    const ctx = this.monitorContext(config);
    if (flags.once) {
      const failing = await checkOnce(ctx);
      this.log(failing.length === 0 ? "All sources healthy." : `Failing: ${failing.join(", ")}`);
      if (failing.length > 0) this.exit(1);
      return;
    }
    await monitorSources(ctx, this.shutdownSignal());
  }
}
