"""Fail if any distribution installed in this environment ships compiled code.

ADR 0001 requires every runtime dependency to be pure Python. Run this with the
interpreter of a fresh environment into which only the Arcflow wheel was
installed, so every distribution it sees is Arcflow or a runtime dependency.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from importlib import metadata


def _is_pure_tag(tag: str) -> bool:
    interpreters, abi, platform = tag.split("-")
    return (
        all(i.startswith("py") for i in interpreters.split("."))
        and abi == "none"
        and platform == "any"
    )


def find_impure(wheels: Mapping[str, str | None]) -> list[tuple[str, str]]:
    """Return (name, reason) for each distribution that is not a pure wheel.

    `wheels` maps a distribution name to the text of its `WHEEL` metadata file,
    or None when it has none (installed from something other than a wheel).
    """
    impure: list[tuple[str, str]] = []
    for name, text in sorted(wheels.items()):
        if text is None:
            impure.append((name, "no WHEEL metadata; cannot verify it is pure Python"))
            continue
        fields = [line.split(":", 1) for line in text.splitlines() if ":" in line]
        tags = [value.strip() for key, value in fields if key.strip() == "Tag"]
        purelib = any(
            key.strip() == "Root-Is-Purelib" and value.strip() == "true" for key, value in fields
        )
        bad_tags = [tag for tag in tags if not _is_pure_tag(tag)]
        if not tags or bad_tags or not purelib:
            impure.append((name, f"tags {tags or 'missing'}, Root-Is-Purelib {purelib}"))
    return impure


def main() -> int:
    wheels = {dist.metadata["Name"]: dist.read_text("WHEEL") for dist in metadata.distributions()}
    impure = find_impure(wheels)
    for name, reason in impure:
        print(f"not pure Python: {name}: {reason}", file=sys.stderr)
    if impure:
        return 1
    print(f"all {len(wheels)} distributions are pure Python: {', '.join(sorted(wheels))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
