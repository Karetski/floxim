"""Whether installed distributions are pure Python.

Used by `floxim doctor` for Floxim's own dependencies and by the CI check
(tools/check_pure_python.py) for a whole environment.
"""

from __future__ import annotations

import re
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


def installed_wheels(names: list[str] | None = None) -> dict[str, str | None]:
    """WHEEL metadata of installed distributions (all, or the named ones)."""
    wanted = {_normalize(n) for n in names} if names is not None else None
    found: dict[str, str | None] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if wanted is None or _normalize(name) in wanted:
            found[name] = dist.read_text("WHEEL")
    return found


def dependency_names(package: str = "floxim") -> list[str]:
    """Names of a package's runtime dependencies, transitively."""
    seen: set[str] = set()
    todo = [package]
    while todo:
        current = todo.pop()
        try:
            requires = metadata.requires(current) or []
        except metadata.PackageNotFoundError:
            continue
        for requirement in requires:
            if "extra ==" in requirement:
                continue
            name = re.split(r"[\s;<>=!~\[(]", requirement, maxsplit=1)[0]
            if _normalize(name) not in seen:
                seen.add(_normalize(name))
                todo.append(name)
    return sorted(seen)


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()
