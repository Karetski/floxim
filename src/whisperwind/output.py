"""The `--json` envelope every command prints."""

from __future__ import annotations

import json
import sys
from typing import Any


def emit_json(ok: bool, *, data: Any = None, error: dict[str, Any] | None = None) -> None:
    document: dict[str, Any] = {"ok": ok}
    if ok:
        document["data"] = data
    else:
        document["error"] = error
    json.dump(document, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def error(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"code": code, "message": message, "details": details}
