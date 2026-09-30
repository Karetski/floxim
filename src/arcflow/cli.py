"""The `arcflow` command line (spec §9)."""

from __future__ import annotations

import argparse
import sys
import traceback
from collections.abc import Sequence

from arcflow import __version__

# Spec §9.2: an unexpected exception is a bug in Arcflow.
EXIT_INTERNAL_ERROR = 70

ISSUES_URL = "https://github.com/Karetski/arcflow/issues"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arcflow",
        description="Terminal-first workflow orchestrator for AI coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"arcflow {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        parser = build_parser()
        parser.parse_args(argv)
        parser.print_help()
        return 0
    except Exception as exc:
        traceback.print_exc()
        print(
            f"arcflow: internal error: {exc}\n"
            f"This is a bug in Arcflow. Please report it at {ISSUES_URL} "
            "with the traceback above.",
            file=sys.stderr,
        )
        return EXIT_INTERNAL_ERROR


def run() -> None:
    """Console-script entry point for `arcflow` and `arcf`."""
    sys.exit(main())
