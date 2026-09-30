"""Build the single-file `floxim.pyz` from a built wheel (ADR 0001).

Usage: python tools/build_zipapp.py dist/floxim-<version>-py3-none-any.whl dist/floxim.pyz

The wheel and its runtime dependencies are installed into a staging directory
with `uv pip install --target`, which is then zipped with the standard library's
`zipapp`. Because every dependency is pure Python, the result runs on any
Python 3.10+ interpreter.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import zipapp
from pathlib import Path


def build(wheel: Path, output: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "app"
        subprocess.run(
            ["uv", "pip", "install", "--quiet", "--target", str(staging), str(wheel)],
            check=True,
        )
        for cache in staging.rglob("__pycache__"):
            shutil.rmtree(cache)
        output.parent.mkdir(parents=True, exist_ok=True)
        zipapp.create_archive(
            staging,
            output,
            interpreter="/usr/bin/env python3",
            main="floxim.cli:run",
            compressed=True,
        )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    build(Path(argv[0]), Path(argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
