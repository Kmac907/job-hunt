"""Command-line entry point."""

import argparse
import sys
from pathlib import Path

from .codex_adapter import CodexAdapterError, codex_preflight
from .config import ConfigError, preflight
from .pipeline import Pipeline, PipelineError
from .storage import StorageError

DISCLOSURE = "Disclosure: job data and resume content will be sent to external Codex processing when inference runs."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job-hunt")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="validate configuration and local prerequisites")
    doctor.add_argument("--config", type=Path, default=Path("config.yaml"))
    run = commands.add_parser("run", help="start an offline saved-fixture run")
    run.add_argument("--config", type=Path, default=Path("config.yaml"))
    run.add_argument("--jobs", "--fixture", "--fixtures", dest="jobs", type=Path)

    profile = commands.add_parser("profile", help="review-gated candidate profile commands")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    approve = profile_commands.add_parser("approve", help="approve the exact reviewed profile")
    approve.add_argument("run_id", nargs="?")
    approve.add_argument("--run", "--run-id", dest="run_id_option")
    approve.add_argument("--version", "--profile-version", dest="profile_version")
    approve.add_argument("--config", type=Path, default=Path("config.yaml"))

    for name, help_text in (
        ("resume", "continue an approved offline run"),
        ("report", "regenerate reports from a saved run"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("run_id", nargs="?")
        command.add_argument("--run", "--run-id", dest="run_id_option")
        command.add_argument("--config", type=Path, default=Path("config.yaml"))
        if name == "report":
            command.add_argument("--output", type=Path)
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
    try:
        if args.command == "run":
            print(DISCLOSURE, flush=True)
            manifest = Pipeline(args.config, args.jobs).run()
            print(
                f"run: {manifest.checkpoint} (run_id={manifest.run_id}, "
                f"profile_version={manifest.output_metadata['profile_version']})"
            )
            return 0
        run_id = args.run_id_option or args.run_id
        if not run_id:
            raise PipelineError("a run ID is required")
        pipeline = Pipeline(args.config)
        if args.command == "profile" and args.profile_command == "approve":
            approval = pipeline.approve_profile(run_id, args.profile_version)
            print(f"profile: approved (run_id={run_id}, profile_version={approval.profile_hash})")
            return 0
        if args.command == "resume":
            print(DISCLOSURE, flush=True)
            manifest = pipeline.resume(run_id)
            print(f"resume: {manifest.status.value} (run_id={run_id})")
            return 0
        paths = pipeline.report(run_id, args.output)
        print("report: " + ", ".join(str(path) for path in paths.values()))
        return 0
    except (ConfigError, PipelineError, StorageError, CodexAdapterError, ValueError) as exc:
        print(f"{args.command}: ERROR: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
