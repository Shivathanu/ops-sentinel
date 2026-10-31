"""`sentinel-validate [path]`: checks workspace specs offline, without touching Exasol, and prints what to wire up.

Exit code 0 means the api and worker will accept the spec. Useful in CI on the repository that holds a team's spec.
"""
import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from ops_sentinel.channels.router import plan_routes
from ops_sentinel.config import settings
from ops_sentinel.core.models import Incident, IncidentStatus, Severity, utcnow
from ops_sentinel.core.spec import SpecError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sentinel-validate", description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", default=settings.spec_file or "sentinel.yaml",
                        help="a spec file or a directory of specs (default: OPS_SENTINEL_SPEC_FILE or sentinel.yaml)")
    parser.add_argument("--base-url", default="http://localhost:8000", help="engine URL used in the printed ingest URLs")
    args = parser.parse_args(argv)

    # The project's .env normally sits next to the spec (docker compose reads it from there too). Variables already
    # set in the environment win.
    spec_dir = Path(args.path) if Path(args.path).is_dir() else Path(args.path).parent
    if (spec_dir / ".env").is_file():
        load_dotenv(spec_dir / ".env", override=False)

    from ops_sentinel.platform import build_platform

    try:
        platform = build_platform(args.path, connect=_no_database)
    except SpecError as exc:
        print(exc, file=sys.stderr)
        return 1
    problems = platform.validate()
    if problems:
        print("invalid configuration:\n  - " + "\n  - ".join(problems), file=sys.stderr)
        return 1

    for entry in platform.workspaces.entries():
        spec = entry.loaded.spec
        print(f"workspace {spec.workspace} ({spec.environment}) from {entry.path}: OK")
        for source in spec.sources:
            where = f"runner '{source.runner}'" if source.runner else f"POST {args.base_url}/v1/ingest/{spec.workspace}/{source.name}"
            auth = "source token" if source.options.get("token") else "engine API key"
            print(f"  source  {source.name:<20} {source.type:<16} {where}" + ("" if source.runner else f"  [{auth}]"))
        for severity in (Severity.CRITICAL, Severity.MEDIUM):
            probe = Incident(incident_id="-", workspace_id=spec.workspace, fingerprint="-", title="-", severity=severity,
                             status=IncidentStatus.OPEN, opened_at=utcnow(), updated_at=utcnow())
            plan = plan_routes(spec, probe)
            print(f"  route   a {severity.value} incident -> notify {list(plan.notify) or '-'}, approve {plan.approve or '-'}")
    return 0


def _no_database():  # pragma: no cover - validation never opens a connection
    raise RuntimeError("sentinel-validate does not connect to Exasol")


if __name__ == "__main__":
    sys.exit(main())
