import path from "node:path";
import { createInterface } from "node:readline/promises";
import { Flags } from "@oclif/core";
import { SentinelCommand } from "../base.js";
import { UsageError } from "../lib/config.js";
import {
  buildProject,
  conflicts,
  DEFAULT_MODELS,
  LISTENERS,
  PROVIDERS,
  writeProject,
  type ListenerKind,
  type Provider,
  type ScaffoldOptions,
} from "../scaffold/index.js";

export default class Init extends SentinelCommand {
  static override summary = "Create a project: config, secrets, business context and Docker Compose for the engine";
  static override description =
    "Writes sentinel.config.json, .env (generated API key and webhook secret, mode 0600), business-context.md, docker-compose.yml, .gitignore and a README. It never overwrites existing files unless --force is given.";
  static override examples = [
    "<%= config.bin %> init",
    "<%= config.bin %> init --yes --provider ollama --listener command --listener webhook",
  ];
  static override flags = {
    provider: Flags.string({ description: "triage model provider", options: [...PROVIDERS] }),
    model: Flags.string({ description: "model name (default depends on the provider)" }),
    listener: Flags.string({ description: "listener to include; repeat for several", options: [...LISTENERS], multiple: true }),
    "slack-channel": Flags.string({ description: "Slack channel id for approvals", default: "" }),
    "slack-approvers": Flags.string({ description: "comma-separated Slack user ids allowed to approve", default: "" }),
    "engine-image": Flags.string({ description: "engine image to run", default: "ops-sentinel:latest" }),
    "engine-build": Flags.string({ description: "path to the engine source; compose builds the image from it" }),
    yes: Flags.boolean({ char: "y", description: "accept defaults without asking" }),
    force: Flags.boolean({ description: "overwrite existing files" }),
  };

  async run(): Promise<void> {
    const { flags } = await this.parse(Init);
    const interactive = !flags.yes && Boolean(process.stdin.isTTY);
    let provider = flags.provider as Provider | undefined;
    let listeners = flags.listener as ListenerKind[] | undefined;
    let model = flags.model;

    if (interactive && (!provider || !listeners)) {
      const rl = createInterface({ input: process.stdin, output: process.stdout });
      try {
        provider ??= (await choose(rl, "Triage model provider", PROVIDERS, "openai")) as Provider;
        listeners ??= await chooseMany(rl, "Listeners to include", LISTENERS, ["command", "webhook"]);
        model ??= (await rl.question(`Model [${DEFAULT_MODELS[provider]}]: `)).trim() || undefined;
      } finally {
        rl.close();
      }
    }
    provider ??= "openai";
    listeners ??= ["command", "webhook"];

    const options: ScaffoldOptions = {
      provider,
      model: model ?? DEFAULT_MODELS[provider],
      listeners,
      engineImage: flags["engine-image"],
      slackChannel: flags["slack-channel"],
      slackApprovers: flags["slack-approvers"],
      ...(flags["engine-build"] && { engineBuild: flags["engine-build"] }),
    };
    const root = this.projectDir(flags.dir);
    const files = buildProject(options);
    const existing = conflicts(root, files);
    if (existing.length > 0 && !flags.force) {
      throw new UsageError(`these files already exist: ${existing.join(", ")}. Use --force to overwrite them.`);
    }
    writeProject(root, files);
    this.log(`Created in ${root}:`);
    for (const file of files) this.log(`  ${path.join(file.path)}`);
    this.log("\nNext: fill in the blanks in .env, then run 'sentinel doctor', 'sentinel up' and 'sentinel watch'.");
  }
}

async function choose(rl: ReturnType<typeof createInterface>, label: string, options: readonly string[], fallback: string): Promise<string> {
  const answer = (await rl.question(`${label} (${options.join("/")}) [${fallback}]: `)).trim();
  if (answer === "") return fallback;
  if (!options.includes(answer)) throw new UsageError(`'${answer}' is not one of: ${options.join(", ")}`);
  return answer;
}

async function chooseMany<T extends string>(rl: ReturnType<typeof createInterface>, label: string, options: readonly T[], fallback: T[]): Promise<T[]> {
  const answer = (await rl.question(`${label} (${options.join(",")}) [${fallback.join(",")}]: `)).trim();
  if (answer === "") return fallback;
  const picked = answer.split(",").map((part) => part.trim()).filter(Boolean);
  const unknown = picked.filter((part) => !options.includes(part as T));
  if (unknown.length > 0) throw new UsageError(`unknown listener(s): ${unknown.join(", ")}`);
  return picked as T[];
}
