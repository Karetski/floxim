"""Process exit codes, stable and documented (spec §9.2)."""

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    RUN_FAILED = 1
    USAGE = 2
    INVALID = 3
    WAITING = 4
    CANCELLED = 5
    NOT_FOUND = 6
    CONFLICT = 7
    DETACHED = 8
    INTERNAL = 70
