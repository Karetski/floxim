"""`floxim flow <op>`: structured edits of a flow file."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from floxim import edit
from floxim.cli.common import print_problems, stderr
from floxim.exitcodes import ExitCode
from floxim.output import emit_json, error


def _value(text: str) -> Any:
    """A value from the command line, read as YAML (`3` is a number, `[a, b]` a list)."""
    return YAML(typ="safe", pure=True).load(text)


def add_parsers(commands: Any, common: argparse.ArgumentParser) -> None:
    flow = commands.add_parser("flow", help="edit a flow file", description="Structured edits.")
    ops = flow.add_subparsers(dest="flow_op", metavar="<op>", required=True)

    add = ops.add_parser("add-node", parents=[common], help="add a node")
    add.add_argument("flow")
    add.add_argument("node")
    add.add_argument("--type", required=True, dest="node_type")
    add.add_argument("--after", help="insert after this node (default: last)")
    add.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", dest="fields")
    add.set_defaults(handler=_run(_add_node))

    rm = ops.add_parser("rm-node", parents=[common], help="remove a node")
    rm.add_argument("flow")
    rm.add_argument("node")
    rm.add_argument("--remove-edges", action="store_true", help="also remove edges to it")
    rm.set_defaults(
        handler=_run(lambda ed, a: edit.remove_node(ed, a.node, remove_edges=a.remove_edges))
    )

    rename = ops.add_parser("rename-node", parents=[common], help="rename a node everywhere")
    rename.add_argument("flow")
    rename.add_argument("old")
    rename.add_argument("new")
    rename.set_defaults(handler=_run(lambda ed, a: edit.rename_node(ed, a.old, a.new)))

    set_ = ops.add_parser("set", parents=[common], help="set a node field")
    set_.add_argument("flow")
    set_.add_argument("node")
    set_.add_argument("path", help="dotted path, e.g. retry.max_attempts")
    set_.add_argument("value", help="a YAML value")
    set_.set_defaults(
        handler=_run(lambda ed, a: edit.set_field(ed, a.node, a.path, _value(a.value)))
    )

    unset = ops.add_parser("unset", parents=[common], help="remove a node field")
    unset.add_argument("flow")
    unset.add_argument("node")
    unset.add_argument("path")
    unset.set_defaults(handler=_run(lambda ed, a: edit.unset_field(ed, a.node, a.path)))

    connect = ops.add_parser("connect", parents=[common], help="add an edge")
    connect.add_argument("flow")
    connect.add_argument("source")
    connect.add_argument("target")
    connect.add_argument("--when", help="condition (default: the default route)")
    connect.add_argument("--on-error", action="store_true", help="edit on_error instead of next")
    connect.add_argument("--position", type=int, help="case position")
    connect.set_defaults(
        handler=_run(
            lambda ed, a: edit.connect(
                ed, a.source, a.target, when=a.when, on_error=a.on_error, position=a.position
            )
        )
    )

    disconnect = ops.add_parser("disconnect", parents=[common], help="remove an edge")
    disconnect.add_argument("flow")
    disconnect.add_argument("source")
    disconnect.add_argument("target")
    disconnect.add_argument("--on-error", action="store_true")
    disconnect.set_defaults(
        handler=_run(lambda ed, a: edit.disconnect(ed, a.source, a.target, on_error=a.on_error))
    )


def _add_node(ed: edit.Editable, args: argparse.Namespace) -> None:
    fields = {}
    for pair in args.fields:
        key, sep, raw = pair.partition("=")
        if not sep:
            raise edit.EditError(f"--set needs KEY=VALUE, got {pair!r}")
        fields[key] = _value(raw)
    edit.add_node(ed, args.node, args.node_type, after=args.after, fields=fields)


def _run(operation: Any) -> Any:
    def handler(args: argparse.Namespace) -> int:
        path = Path(args.flow)
        try:
            ed = edit.open_flow(path)
            operation(ed, args)
            report = edit.save(ed)
        except FileNotFoundError:
            return _fail(args, "E-NOT-FOUND", f"no flow file {path}", ExitCode.NOT_FOUND, [])
        except edit.EditConflict as exc:
            return _fail(args, "E-CONFLICT", str(exc), ExitCode.CONFLICT, [])
        except edit.EditError as exc:
            code = ExitCode.INVALID if exc.problems else ExitCode.USAGE
            return _fail(args, "E-EDIT-REFUSED", str(exc), code, exc.problems)
        problems = [p.to_json() for p in report.problems]
        if args.json:
            emit_json(True, data={"file": str(path), "problems": problems})
        else:
            stderr(f"updated {path}")
            print_problems(report.problems)
        return ExitCode.OK

    return handler


def _fail(
    args: argparse.Namespace, code: str, message: str, exit_code: int, problems: list[Any]
) -> int:
    if args.json:
        emit_json(False, error=error(code, message, {"problems": [p.to_json() for p in problems]}))
    else:
        stderr(f"floxim: {message}")
        print_problems(problems)
    return exit_code
