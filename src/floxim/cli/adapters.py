"""`floxim adapter test` (spec §8.8)."""

from __future__ import annotations

import argparse
import os
from typing import Any

from floxim.adapters.registry import UnknownAdapter
from floxim.cli.common import project_context, stderr
from floxim.exitcodes import ExitCode
from floxim.output import emit_json, error


def add_parsers(commands: Any, common: argparse.ArgumentParser) -> None:
    adapter = commands.add_parser("adapter", help="adapter tools", description="Adapter tools.")
    sub = adapter.add_subparsers(dest="adapter_command", metavar="<command>", required=True)
    test = sub.add_parser(
        "test",
        parents=[common],
        help="run the conformance suite against an adapter",
        description="Run the adapter conformance suite (spec §8.8).",
    )
    test.add_argument("name", help="adapter name")
    test.add_argument(
        "--live", action="store_true", help="also call the real harness (costs money)"
    )
    test.set_defaults(handler=cmd_adapter_test)


def cmd_adapter_test(args: argparse.Namespace) -> int:
    from floxim import conformance

    context = project_context()
    try:
        results = conformance.run_offline(args.name, context.root)
        if args.live:
            results += conformance.run_live(args.name, context.root, dict(os.environ))
    except (UnknownAdapter, ValueError) as exc:
        if args.json:
            emit_json(False, error=error("E-NOT-FOUND", str(exc)))
        else:
            stderr(f"floxim: {exc}")
        return ExitCode.NOT_FOUND
    passed = all(r.passed for r in results)
    if args.json:
        data = {"adapter": args.name, "cases": [r.to_json() for r in results]}
        if passed:
            emit_json(True, data=data)
        else:
            emit_json(False, error=error("E-CONFORMANCE", "some cases failed", data))
    else:
        for result in results:
            mark = "✓" if result.passed else "✗"
            print(f"{mark} {result.case}")
            for problem in result.problems:
                print(f"    {problem}")
        failed = sum(1 for r in results if not r.passed)
        stderr(f"{len(results) - failed}/{len(results)} cases passed")
    return ExitCode.OK if passed else ExitCode.RUN_FAILED
