"""Write a Markdown report of the TUI snapshots a change adds, changes or removes.

Each snapshot is the SVG that Rich exports for a screen; the report shows it as plain
text, so a pull request comment can show the screens inline. Colour and styling are
lost; the full-colour comparison is pytest-textual-snapshot's HTML report.

    python tools/tui_report.py BASE_REF --status passed|failed [--run-url URL]
"""

from __future__ import annotations

import argparse
import difflib
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from rich.cells import cell_len

SNAPSHOTS = "tests/__snapshots__"
MARKER = "<!-- whisperwind-tui-report -->"
# GitHub rejects comments over 65536 characters.
LIMIT = 60000

_SVG = "{http://www.w3.org/2000/svg}"
_LINE = re.compile(r"-line-(\d+)\)$")


def svg_to_text(svg: str) -> str:
    """The characters of a Rich-exported terminal SVG, laid out on their rows and columns."""
    runs: list[tuple[int, float, str]] = []
    cell = 0.0
    for element in ET.fromstring(svg).iter(f"{_SVG}text"):
        line = _LINE.search(element.get("clip-path", ""))
        if line and element.text:
            runs.append((int(line[1]), float(element.get("x", 0)), element.text))
            # Rich sets textLength to the run's character count times the cell width.
            cell = cell or float(element.get("textLength", 0)) / len(element.text)
    if not runs:
        return ""
    rows: list[str] = [""] * (max(row for row, _, _ in runs) + 1)
    for row, x, text in sorted(runs):
        column = round(x / cell)
        rows[row] += " " * max(column - cell_len(rows[row]), 0) + text.replace("\xa0", " ")
    return "\n".join(row.rstrip() for row in rows).rstrip("\n")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def changed_snapshots(merge_base: str) -> list[tuple[str, str]]:
    """(status, path) for each snapshot that differs from `merge_base`: A added,
    M modified, D deleted."""
    out = _git("diff", "--no-renames", "--name-status", merge_base, "--", SNAPSHOTS)
    return [(line[0], line.split("\t", 1)[1]) for line in out.splitlines()]


def _name(path: str) -> str:
    return path.removeprefix(f"{SNAPSHOTS}/").rsplit(".", 1)[0]


def _block(kind: str, text: str) -> str:
    return f"```{kind}\n{text}\n```"


def _details(summary: str, body: str, *, open_: bool = True) -> str:
    return f"<details{' open' if open_ else ''}><summary>{summary}</summary>\n\n{body}\n</details>"


def _section(status: str, path: str, base: str) -> str:
    title = f"<code>{_name(path)}</code>"
    if status == "D":
        return f"- {title} — removed"
    new = svg_to_text(Path(path).read_text(encoding="utf-8"))
    if status == "A":
        return _details(f"{title} — added", _block("text", new))
    old = svg_to_text(_git("show", f"{base}:{path}"))
    diff = "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=1))
    diff = diff.split("\n", 2)[2] if diff else "(only colour or styling changed)"
    full = _details("Full screen", _block("text", new), open_=False)
    return _details(f"{title} — changed", f"{_block('diff', diff)}\n\n{full}")


def report(base: str, status: str, run_url: str | None) -> str:
    link = f" · [run]({run_url})" if run_url else ""
    if status == "passed":
        head = f"✅ Tests passed{link}"
    else:
        head = f"❌ Tests failed{link}. The full-colour diff is in the `snapshot-report` artifact."
    merge_base = _git("merge-base", base, "HEAD").strip()
    changes = changed_snapshots(merge_base)
    if not changes:
        return f"{MARKER}\n## TUI snapshots\n\n{head}\n\nNo screen changed."
    parts = [f"{MARKER}\n## TUI snapshots\n\n{head}\n\n{len(changes)} screen(s) changed."]
    for i, (change, path) in enumerate(changes):
        section = _section(change, path, merge_base)
        if sum(map(len, parts)) + len(section) > LIMIT:
            rest = ", ".join(f"<code>{_name(p)}</code>" for _, p in changes[i:])
            parts.append(f"Too long to show the rest: {rest}")
            break
        parts.append(section)
    return "\n\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("base", help="the branch the change will merge into, e.g. origin/main")
    parser.add_argument("--status", choices=["passed", "failed"], required=True)
    parser.add_argument("--run-url")
    args = parser.parse_args(argv)
    print(report(args.base, args.status, args.run_url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
