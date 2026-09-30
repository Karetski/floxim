# 0013. Project name: Arcflow

- Status: Accepted
- Date: 2026-09-29
- Decided by: Alexey
- Supersedes: [0009](0009-name-deferred.md)

## Context

"Loom" was a working name (ADR 0009). It collides with Atlassian's Loom product and many existing repositories, and it is generic. The final name needed to be short, clean and modern, and free where Arcflow is distributed: PyPI (ADR 0001 ships via `pipx` / `uv tool install`) and GitHub.

## Decision

The project is named **Arcflow**: arcs (graph edges) plus flow, matching the model of flows as graphs of nodes and edges.

- Python distribution and import package: `arcflow`.
- CLI command: `arcflow`, plus `arcf` as a short alias installed as a second console script for the same entry point.
- Project directory `.arcflow/`, user config `$XDG_CONFIG_HOME/arcflow/`, environment variables `ARCFLOW_*`, entry-point group `arcflow.adapters`, flow format key `arcflow: 1`.
- The executable-adapter protocol is the **Arcflow Adapter Protocol (AAP)**, `protocol: aap`.
- Repository: `github.com/Karetski/arcflow`.

## Consequences

- At decision time (2026-09-29), `arcflow` was unused on PyPI and GitHub, and `arcf` was unused on PyPI and Homebrew (an npm package of that name was unpublished in 2022). The PyPI name is not reserved until the first upload, so publish a placeholder early if it matters.
- No domain or trademark check was done; do one before the first public release (M8).
- The short alias is the only second name. Directories, environment variables and the package use the full name, so there is one spelling to search for.

## Alternatives considered

Rejected as taken on PyPI or crowded on GitHub: `relay`, `baton`, `braid`, `segue`, `tempo`, `cadence` (also Uber's workflow engine). Offered and not chosen: Teamster, Whiffletree, Muleteer, Switchman, Cuelist, Relai, Ondeck.
