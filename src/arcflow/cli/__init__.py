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
    from arcflow.config import find_project_root, load_config
    from arcflow.validate import validate

    root = find_project_root(Path.cwd())
    config, config_problems = load_config(root)
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
    from arcflow.config import config_json_schema
    from arcflow.flowspec import flow_json_schema

    schema = {
        "flow": flow_json_schema,
        "config": config_json_schema,
        "harness": harness_json_schema,
    }[args.which]()
    if args.json:
        emit_json(True, data=schema)
    else:
        print(json.dumps(schema, indent=2))
    return ExitCode.OK


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print one JSON document to stdout")

    parser = argparse.ArgumentParser(
        prog="arcflow",
        description="Terminal-first workflow orchestrator for AI coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"arcflow {__version__}")
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
    schema.add_argument("which", choices=["flow", "config", "harness"], help="which schema")
    schema.set_defaults(handler=_cmd_schema)

    from arcflow.cli import runs

    runs.add_parsers(commands, common)

    from arcflow.cli import adapters, inspection

    adapters.add_parsers(commands, common)
    inspection.add_parsers(commands, common)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
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
