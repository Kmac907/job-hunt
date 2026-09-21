"""Command-line entry point."""

import argparse
import sys
from pathlib import Path

from .codex_adapter import CodexAdapterError, codex_preflight
from .config import ConfigError, preflight

DISCLOSURE = "Disclosure: job data and resume content will be sent to external Codex processing when inference runs."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job-hunt")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="validate configuration and local prerequisites")
    doctor.add_argument("--config", type=Path, default=Path("config.yaml"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        print(DISCLOSURE, flush=True)
        try:
            config, companies = preflight(args.config)
            codex = codex_preflight(timeout_seconds=min(config.runtime.timeout_seconds, 10))
        except (ConfigError, CodexAdapterError) as exc:
            print(f"doctor: ERROR: {exc}", file=sys.stderr)
            return 1
        print(
            f"doctor: OK ({len(companies.companies)} companies, model {config.runtime.model}, "
            f"{codex.version})"
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
