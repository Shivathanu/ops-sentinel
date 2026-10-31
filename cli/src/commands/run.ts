import { existsSync } from "node:fs";
import path from "node:path";
import { Flags } from "@oclif/core";
import { DEFAULT_CONFIG, SentinelCommand } from "../base.js";
import { parseConfig, UsageError, type Config } from "../lib/config.js";
import { defaultName, shellQuote } from "../lib/shell.js";
import { parseFixFlags, runWrapped } from "../lib/wrap.js";

export default class Run extends SentinelCommand {
  static override summary = "Run one command and report it if it fails";
  static override description =
    "Wraps a single command: its output and exit code pass through. On failure the incident goes to the engine, and with --wait this stays until a person decides and any approved fix has run.";
  static override strict = false;
  static override examples = [
    "<%= config.bin %> run --name nightly-export -- ./export.sh --full",
    "<%= config.bin %> run --wait --fix-id rerun --fix-run './export.sh --retry' -- ./export.sh",
  ];
  static override flags = {
    name: Flags.string({ description: "source name shown in incidents (default: the command's file name)" }),
    config: Flags.string({ char: "c", description: "config to take engine settings and a matching source's fix from" }),
    "fix-id": Flags.string({ description: "id of the fix to offer (needs --fix-run)" }),
    "fix-run": Flags.string({ description: "command that performs the fix (needs --fix-id)" }),
    wait: Flags.boolean({ description: "stay until a person decides and any approved fix has run" }),
  };

  async run(): Promise<void> {
    const { flags, argv } = await this.parse(Run);
    const words = argv.map(String);
    if (words.length === 0) throw new UsageError("nothing to run; put the command after '--'");
    const command = words.length === 1 ? (words[0] as string) : words.map(shellQuote).join(" ");
    const name = flags.name ?? defaultName(words[0] as string);
    const config = this.runConfig(flags.dir, flags.config);
    const configured = config.sources.find((source) => source.name === name);
    const fix = parseFixFlags(flags["fix-id"], flags["fix-run"]) ?? configured?.fix;
    const code = await runWrapped(
      this.monitorContext(config),
      { name, command, cwd: process.cwd(), wait: flags.wait, ...(fix && { fix }) },
      this.shutdownSignal(),
    );
    if (code !== 0) this.exit(code);
  }

  /** A config file, or two environment variables for a one-off wrap in CI. */
  private runConfig(dir: string, configFlag: string | undefined): Config {
    const root = this.projectDir(dir);
    const file = path.resolve(root, configFlag ?? DEFAULT_CONFIG);
    if (configFlag !== undefined || existsSync(file)) return this.loadProjectConfig(dir, configFlag, false);
    const env = this.projectEnv(dir);
    const url = env["SENTINEL_ENGINE_URL"];
    if (!url) {
      throw new UsageError(`no ${DEFAULT_CONFIG} here; pass --config or set SENTINEL_ENGINE_URL and OPS_SENTINEL_API_KEY`);
    }
    return parseConfig({ engine: { url } }, { env, baseDir: root, requireSources: false });
  }
}
