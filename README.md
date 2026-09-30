# Arcflow

Arcflow is a terminal-first, harness-agnostic workflow orchestrator for AI coding agents. You define flows (graphs of agent, shell, condition, human and sleep nodes) in YAML files, and Arcflow runs them durably by driving existing harnesses such as Claude Code and Codex CLI in headless mode.

**Status:** early development; nothing is usable yet. See [`PROJECT_BRIEF.md`](PROJECT_BRIEF.md) for goals, [`docs/spec.md`](docs/spec.md) for the behaviour being built, and [`docs/milestones.md`](docs/milestones.md) for the plan.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync                 # create .venv with the locked dev dependencies
uv run pytest           # tests
uv run ruff check .     # lint
uv run ruff format .    # format
uv run mypy             # type check
```

## License

MIT, see [`LICENSE`](LICENSE).
