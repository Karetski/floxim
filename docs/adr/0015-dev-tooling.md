# 0015. CLI library, packaging and dev tooling

- Status: Accepted
- Date: 2026-09-29
- Decided by: Alexey (milestone M0)
- Context: ADR 0001, spec §9, `docs/milestones.md` M0

## Context

ADR 0001 fixed Python 3.10+ with pure-Python runtime dependencies and left the CLI library open (`typer` or `argparse`). M0 also needs a build backend, a type checker and a way to manage the development environment. The pure-Python rule applies to what users install; tools used only during development may ship compiled code.

## Decision

- **CLI:** `argparse` from the standard library. No runtime dependency, the fastest startup for scripts that poll `arcflow status --json`, and full control over output and the exit codes of spec §9.2.
- **Build backend:** `hatchling`. It is pure Python, so building from the sdist on a restricted mirror needs no compiled code.
- **Type checking:** `mypy --strict` in CI from the first commit.
- **Lint and format:** `ruff`.
- **Development environment:** `uv`, with `uv.lock` committed and CI running `uv sync --locked`. Users still install with `pipx` or `uv tool install` (ADR 0001); `uv` is not required to use Arcflow.

## Consequences

- Each command's arguments are declared by hand; there is no type-hint-driven command generation.
- Contributors need `uv` for the reproducible environment; dev dependency upgrades are explicit lockfile changes.

## Alternatives considered

- `typer` or `click`: less boilerplate, but more dependencies at startup and exit codes that must be overridden to match §9.2.
- `uv_build`: fast, but a native binary is needed to build from the sdist. `setuptools`: pure Python but heavier configuration.
- `pyright`, `ty`: faster checkers; `mypy` is the most mature, and speed does not matter at this size.
