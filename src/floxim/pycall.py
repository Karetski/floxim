"""Child-process entry point for `python` nodes.

Run as a script by path, with any Python 3 interpreter: it imports nothing from
Floxim, so `interpreter:` can name a Python that does not have Floxim
installed. It reads one JSON request on stdin:

    {"call": "pkg.module:function", "args": {...}, "ctx": {...},
     "project_root": "/path", "result_file": "/path/result.json"}

imports the module with the project root first on sys.path, calls the function
with `args` as keyword arguments (plus `ctx` when the function accepts it), and
writes `{"ok": true, "value": ...}` or `{"ok": false, "error": ..., "traceback": ...}`
to the result file. The exit code is 0 whenever a result file was written.
"""

import collections
import importlib
import inspect
import json
import sys
import traceback

# Read-only context passed to functions that accept `ctx`.
Context = collections.namedtuple(
    "Context", ["run_id", "node_id", "visit", "artifacts_dir", "workdir"]
)


def main() -> int:
    request = json.load(sys.stdin)
    sys.path.insert(0, request["project_root"])
    module_name, _, function_name = request["call"].partition(":")
    try:
        function = getattr(importlib.import_module(module_name), function_name)
        kwargs = dict(request["args"])
        parameters = inspect.signature(function).parameters
        accepts_ctx = "ctx" in parameters or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
        )
        if accepts_ctx:
            kwargs["ctx"] = Context(**request["ctx"])
        value = function(**kwargs)
        text = json.dumps({"ok": True, "value": value})
    except BaseException as exc:  # report everything, including SystemExit
        text = json.dumps(
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        )
    with open(request["result_file"], "w", encoding="utf-8") as handle:
        handle.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
