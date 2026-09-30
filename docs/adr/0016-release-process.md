# 0016. Release process

- Status: Accepted
- Date: 2026-09-29
- Decided by: Alexey (milestone M8)
- Context: ADR 0001, ADR 0013, `docs/milestones.md` M8

## Context

ADR 0001 ships Arcflow as a pure-Python wheel installed with `pipx` or `uv tool install`, plus a single-file zipapp. M8 needs a repeatable way to publish them, a version scheme, and a changelog.

## Decision

- **Trigger:** pushing a tag `vX.Y.Z` runs `.github/workflows/release.yml`. Nothing else publishes.
- **Checks before publishing:** the tag equals `__version__` in `src/arcflow/__init__.py` (the single source of the version), `CHANGELOG.md` has an entry for it, the test suite passes, every runtime dependency is pure Python, and the zipapp runs on Python 3.10.
- **PyPI:** the wheel and sdist are uploaded with PyPI trusted publishing (OpenID Connect from the `pypi` GitHub environment). No PyPI token is stored anywhere.
- **GitHub release:** created after the PyPI upload, with the wheel, sdist and `arcflow.pyz` attached and the changelog entry as its notes.
- **Versions:** Semantic Versioning, starting at 0.1.0. Before 1.0, a minor version may change the CLI. The flow format is versioned separately by the `arcflow:` key (spec §3.2).
- **Changelog:** `CHANGELOG.md` in the Keep a Changelog format, updated in the commit that sets the version.

The steps are in `docs/releasing.md`.

## Consequences

- Publishing needs one-time setup on PyPI (a trusted publisher for this repository, workflow and environment) and a `pypi` environment in the GitHub repository, which can require a reviewer's approval before each upload.
- A release cannot be published from a laptop by accident, and a tag that does not match the version fails before anything is uploaded.
- A published version cannot be replaced on PyPI; a broken release is fixed with a new patch version.

## Alternatives considered

- A local script running `uv publish` with a token: simpler, but depends on one machine and a long-lived secret.
- Publishing on every push to `main` with a computed dev version: too noisy for a tool people install.
