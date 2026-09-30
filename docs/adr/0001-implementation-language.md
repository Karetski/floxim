# 0001. Implementation language: Python

- Status: Accepted
- Date: 2026-09-30
- Decided by: Alexey
- Context: docs/research.md §4, §6 (row 1), §8.3

## Context

Loom must install easily in enterprise development environments, some of which restrict downloaded executables or compiled code, and it must perform well. Its engine is I/O-bound, spending nearly all its time waiting on harness processes (Claude Code, Codex) that run for minutes. The harness SDKs are thin wrappers around the CLIs, so no language gains a meaningful integration advantage.

Measured on Python 3.11 (research.md §8.3): about 150 ms of CLI startup with the proposed dependencies, about 220 ms to import Textual, and about 20 µs to evaluate a condition expression.

## Decision

Loom is implemented in **Python 3.10+**, with:

- **Pure-Python runtime dependencies only**, so it installs from any internal PyPI mirror with no compiled code. Initial choices: `textual` (TUI), `ruamel.yaml` (round-trip YAML), `fastjsonschema`, `typer` or `argparse` (CLI), and a restricted Python-syntax expression evaluator (`simpleeval` or an `ast` whitelist). File watching uses polling.
- **Distribution** via `pipx install` / `uv tool install`, plus a single-file `.pyz` zipapp for machines with only a system Python.
- **Extension points** (plugin adapters, custom nodes) loaded as ordinary Python packages through entry points.

## Consequences

- Positive: installable where compiled code is blocked; plugins and custom nodes are plain Python; Textual gives a capable TUI.
- Negative: slower CLI startup than a native binary (~150 ms); users can hit Python-version or dependency conflicts, which pipx/uv isolation and the zipapp mitigate; CEL is dropped because its only Python implementation needs native code, so conditions use Python-syntax expressions, and validate-time type checks must be reimplemented against node output schemas.
- CI must fail if any runtime dependency ships compiled code.

## Alternatives considered

- **Go + Bubble Tea v2:** single fast binary and the reference CEL implementation, but ships native code.
- **TypeScript + Ink:** first-party harness SDKs, but needs Node or large compiled bundles.
