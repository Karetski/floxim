"""The `arcflow` command line (spec §9)."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path

from arcflow import __version__
from arcflow.exitcodes import ExitCode
from arcflow.output import emit_json, error

ISSUES_URL = "https://github.com/Karetski/arcflow/issues"


def _cmd_validate(args: argparse.Namespace) -> int:
    from arcflow.cli.common import project_context
    from arcflow.validate import validate

    context = project_context()
    config, config_problems, root = context.config, context.config_problems, context.root
    reports = [validate(Path(p), config=config, workdir=root) for p in args.flows]
    ok = all(r.ok(args.strict) for r in reports) and not any(p.is_error for p in config_problems)
    problems = config_problems + [p for r in reports for p in r.problems]
    if args.json:
        files = [
            {
                "file": str(r.path),
                "valid": r.ok(args.strict),
                "name": r.flow.name if r.flow else None,
            }
            for r in reports
        ]
        payload = {"files": files, "problems": [p.to_json() for p in problems]}
        if ok:
            emit_json(True, data=payload)
        else:
            count = sum(1 for p in problems if p.is_error or (args.strict and p.code[0] == "W"))
            emit_json(False, error=error("E-INVALID-FLOW", f"{count} problem(s) found", payload))
    else:
        for problem in problems:
            print(problem.render())
        for report in reports:
            state = "valid" if report.ok(args.strict) else "invalid"
            print(f"{report.path}: {state}", file=sys.stderr)
    return ExitCode.OK if ok else ExitCode.INVALID


def _cmd_schema(args: argparse.Namespace) -> int:
    from arcflow.adapters.command import harness_json_schema
    from arcflow.cli.schemas import cli_json_schema
    from arcflow.config import config_json_schema
    from arcflow.flowspec import flow_json_schema

    schema = {
        "flow": flow_json_schema,
        "config": config_json_schema,
        "harness": harness_json_schema,
        "cli": cli_json_schema,
    }[args.which]()
    if args.json:
        emit_json(True, data=schema)
    else:
        print(json.dumps(schema, indent=2))
    return ExitCode.OK


def _global_flags(parser: argparse.ArgumentParser) -> None:
    """Flags every command accepts, before or after its name (spec §9.1)."""
    group = parser.add_argument_group("global options")
    quiet = argparse.SUPPRESS
    group.add_argument("--project", metavar="DIR", default=quiet, help="project root")
    group.add_argument("--config", metavar="FILE", default=quiet, help="extra config file")
    group.add_argument("--quiet", action="store_true", default=quiet, help="print less")
    group.add_argument("--verbose", action="store_true", default=quiet, help="print every event")
    group.add_argument("--no-color", action="store_true", default=quiet, help="no colour")
    group.add_argument("--as", dest="as_", metavar="NAME", default=quiet, help="who is acting")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print one JSON document to stdout")
    _global_flags(common)

    parser = argparse.ArgumentParser(
        prog="arcflow",
        description="Terminal-first workflow orchestrator for AI coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"arcflow {__version__}")
    _global_flags(parser)
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    validate = commands.add_parser(
        "validate", parents=[common], help="check flow files", description="Check flow files."
    )
    validate.add_argument("flows", nargs="+", metavar="flow", help="flow file")
    validate.add_argument("--strict", action="store_true", help="treat warnings as errors")
    validate.set_defaults(handler=_cmd_validate)

    schema = commands.add_parser(
        "schema", parents=[common], help="print a JSON Schema", description="Print a JSON Schema."
    )
    schema.add_argument("which", choices=["flow", "config", "harness", "cli"], help="which schema")
    schema.set_defaults(handler=_cmd_schema)

    from arcflow.cli import runs

    runs.add_parsers(commands, common)

    from arcflow.cli import adapters, editing, housekeeping, inspection

    adapters.add_parsers(commands, common)
    editing.add_parsers(commands, common)
    inspection.add_parsers(commands, common)
    housekeeping.add_parsers(commands, common)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        from arcflow.cli.common import set_options

        set_options(args)
        if args.command is None:
            parser.print_help()
            return ExitCode.OK
        return int(args.handler(args))
    except Exception as exc:
        traceback.print_exc()
        print(
            f"arcflow: internal error: {exc}\n"
            f"This is a bug in Arcflow. Please report it at {ISSUES_URL} "
            "with the traceback above.",
            file=sys.stderr,
        )
        return ExitCode.INTERNAL


def run() -> None:
    """Console-script entry point for `arcflow` and `arcf`."""
    sys.exit(main())
