"""Fail if any distribution installed in this environment ships compiled code.

ADR 0001 requires every runtime dependency to be pure Python. Run this with the
interpreter of a fresh environment into which only the Arcflow wheel was
installed, so every distribution it sees is Arcflow or a runtime dependency.
"""

from __future__ import annotations

import sys

from arcflow.purity import find_impure, installed_wheels

__all__ = ["find_impure", "main"]


def main() -> int:
    wheels = installed_wheels()
    impure = find_impure(wheels)
    for name, reason in impure:
        print(f"not pure Python: {name}: {reason}", file=sys.stderr)
    if impure:
        return 1
    print(f"all {len(wheels)} distributions are pure Python: {', '.join(sorted(wheels))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
