# 0017. Project name: Floxim

- Status: Accepted
- Date: 2026-09-30
- Decided by: Alexey
- Supersedes: [0013](0013-name-arcflow.md)

## Context

The project was named Arcflow (ADR 0013). That name was awkward to type, and it was taken: an ERP product holds a registered trademark for it, and another project in the agent-workflow space used it. Arcflow 0.1.0 was never published to PyPI or tagged on GitHub, so a rename had no users, stored runs or published artifacts to migrate.

The new name had to be short, abstract and distinctive, and the exact name had to be free on PyPI.

## Decision

The project is named **Floxim**, an invented word with no meaning of its own.

- Python distribution and import package: `floxim`.
- CLI command: `floxim`, plus `flx` as a short alias installed as a second console script for the same entry point.
- Project directory `.floxim/`, user config `$XDG_CONFIG_HOME/floxim/`, environment variables `FLOXIM_*`, entry-point group `floxim.adapters`, flow format key `floxim: 1`, run-state field `floxim_version`.
- The executable-adapter protocol is the **Agent Adapter Protocol (AAP)**, `protocol: aap`. The protocol name no longer carries the project name, so a future rename leaves the protocol identifier untouched.
- Repository: `github.com/Karetski/floxim`, created fresh with the full history. The `Karetski/arcflow` repository was deleted.
- The rename is a clean break with no compatibility aliases for the old names, because nothing had been released.

ADR 0013's one-spelling rule still applies: directories, environment variables and the package use the full name, and `flx` is the only second name.

## Consequences

- At decision time (2026-09-30), `floxim` and `flx` were unused on PyPI and Homebrew, and `floxim` was unused on npm.
- An unrelated PHP CMS named Floxim exists on GitHub (the `Floxim` organization, last active in 2020). The `github.com/floxim` path belongs to it.
- No domain or trademark check was done. Do one before the first public release.
- PyPI trusted publishing (ADR 0016) must be configured for the `floxim` project name before the first release.

## Alternatives considered

Invented words, words with a suffix, words from other languages and human names were offered and not chosen, among them Taskrail, Kadence, Nilo, Valo, Evra, Torqx, Pivox, Klyx, Virta, Dromos, Orson and Calder. Noxim was considered and rejected because an established Network-on-Chip simulator uses the name.
