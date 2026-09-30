"""Which environment variables reach templates and child processes.

Processes start from an empty environment plus: a base allowlist, the auth
variables of the adapter in use, `env_passthrough` from config, the flow's and
node's `env`, and `FLOXIM_*` run variables. Variables that belong to a parent
agent session are always removed, so a nested harness never inherits it.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping

BASE_ALLOWLIST = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_*", "TERM", "TZ", "TMPDIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "GIT_*",
)  # fmt: skip
BASE_DENY = ("GIT_DIR", "GIT_WORK_TREE")

# Variables of a parent agent session; never passed on.
SESSION_DENYLIST = (
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_AUTO_BACKGROUND_TASKS",
    "CODEX_THREAD_ID",
    "CODEX_SANDBOX*",
)


def _matches(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def allowed(
    source: Mapping[str, str],
    *,
    passthrough: Iterable[str] = (),
    adapter_vars: Iterable[str] = (),
    session_denylist: Iterable[str] = (),
) -> dict[str, str]:
    """Variables of `source` allowed by the base allowlist, the adapter's auth variables
    and `env_passthrough`."""
    allow = (*BASE_ALLOWLIST, *adapter_vars, *passthrough)
    deny = (*BASE_DENY, *SESSION_DENYLIST, *session_denylist)
    return {
        name: value
        for name, value in source.items()
        if _matches(name, allow) and not _matches(name, deny)
    }


def process_environment(
    source: Mapping[str, str],
    *,
    passthrough: Iterable[str] = (),
    adapter_vars: Iterable[str] = (),
    session_denylist: Iterable[str] = (),
    extra: Mapping[str, str] | None = None,
    floxim: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The complete environment for a child process."""
    env = allowed(
        source,
        passthrough=passthrough,
        adapter_vars=adapter_vars,
        session_denylist=session_denylist,
    )
    env.update(extra or {})
    env.update(floxim or {})
    deny = (*SESSION_DENYLIST, *session_denylist)
    return {k: v for k, v in env.items() if not _matches(k, deny)}
