"""Secret references in the workspace spec.

The spec never holds a secret value. It holds a reference that is resolved when the spec is loaded:

    ${env:SLACK_BOT_TOKEN}       an environment variable
    ${file:/run/secrets/slack}   a file's contents, trailing newline stripped (Docker and Kubernetes secrets)

A reference can be the whole value or part of one (`https://hooks.example.com/${env:HOOK_PATH}`). New providers
(Vault, AWS Secrets Manager) register in `PROVIDERS`.
"""
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

REFERENCE = re.compile(r"\$\{(?P<provider>[a-z]+):(?P<key>[^}]+)\}")


class SecretResolutionError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("cannot resolve secret references:\n  - " + "\n  - ".join(problems))
        self.problems = problems


def _from_file(key: str, _env: Mapping[str, str]) -> str:
    return Path(key).read_text(encoding="utf-8").rstrip("\r\n")


def _from_env(key: str, env: Mapping[str, str]) -> str:
    value = env.get(key)
    if value is None or value == "":
        raise KeyError(f"environment variable {key} is not set")
    return value


PROVIDERS: dict[str, Callable[[str, Mapping[str, str]], str]] = {"env": _from_env, "file": _from_file}


def resolve(value: Any, env: Mapping[str, str] | None = None) -> Any:
    """Returns `value` with every reference replaced. Collects every failure before raising, so one run reports all."""
    environment = os.environ if env is None else env
    problems: list[str] = []

    def substitute(match: re.Match[str], where: str) -> str:
        provider, key = match["provider"], match["key"].strip()
        lookup = PROVIDERS.get(provider)
        if lookup is None:
            problems.append(f"{where}: unknown secret provider '{provider}' (known: {', '.join(sorted(PROVIDERS))})")
            return ""
        try:
            return lookup(key, environment)
        except (KeyError, OSError) as exc:
            reason = exc.args[0] if isinstance(exc, KeyError) else str(exc)
            problems.append(f"{where}: {reason}")
            return ""

    def walk(node: Any, where: str) -> Any:
        if isinstance(node, str):
            return REFERENCE.sub(lambda m: substitute(m, where), node)
        if isinstance(node, dict):
            return {k: walk(v, f"{where}.{k}" if where else str(k)) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, f"{where}[{i}]") for i, v in enumerate(node)]
        return node

    resolved = walk(value, "")
    if problems:
        raise SecretResolutionError(problems)
    return resolved


def secret_values(raw: Any, resolved: Any) -> set[str]:
    """Every value that came from a reference, so logs and prompts can redact them."""
    found: set[str] = set()
    if isinstance(raw, str) and REFERENCE.search(raw) and isinstance(resolved, str) and resolved:
        found.add(resolved)
    elif isinstance(raw, dict) and isinstance(resolved, dict):
        for key in raw:
            found |= secret_values(raw[key], resolved.get(key))
    elif isinstance(raw, list) and isinstance(resolved, list):
        for r, v in zip(raw, resolved, strict=False):
            found |= secret_values(r, v)
    return found
