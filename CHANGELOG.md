# Changelog

All notable changes to Arcflow are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). Before 1.0, a minor version may change the CLI; the flow format is versioned separately by the `arcflow:` key.

## [0.1.0] - 2026-09-29

The first release.

### Added

- Flow files (format version 1): `agent`, `shell`, `condition`, `human`, `sleep`, `set`, `python`, `subflow`, `map`, `handoff` and `notify` nodes; routing with `next` and `on_error`; a safe expression and template language; inputs, outputs, defaults, templates and `include`; limits, budgets, retries and timeouts; shared directories and Arcflow-managed git worktrees.
- `arcflow validate` with staged checks, stable problem codes, line and column positions, security lints and `--strict`; published JSON Schemas for flows, config, command adapters and every CLI document.
- A durable runner: an fsynced event log per run, state derived from it, crash-safe resume that never reruns a finished visit, `--reload`, `--from`, `--force` and `--due`, and runs that wait for people with no process alive.
- Harness adapters for Claude Code and Codex CLI, YAML command adapters for any JSON-lines agent CLI, Python entry-point adapters, a deterministic fake adapter, and the `arcflow adapter test` conformance kit.
- The CLI: `run`, `resume`, `respond`, `cancel`, `handoff`, `wait`, `status`, `list`, `logs`, `artifacts`, `flows`, `graph`, `flow` (structured edits that keep comments and formatting), `init`, `adapters`, `gc`, `doctor` and `schema`, with `--json` and stable exit codes. `arcf` is a short alias.
- The terminal UI: runs, run detail with a live graph, answers and run control, and a graph editor that stays in sync with the flow file.
- User guides, the Appendix A example flows, a wheel, an sdist and a single-file `arcflow.pyz`.

[0.1.0]: https://github.com/Karetski/arcflow/releases/tag/v0.1.0
