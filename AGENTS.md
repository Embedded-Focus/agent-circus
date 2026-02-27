# AGENTS.md

## Repository Overview

- Purpose: CLI tool for running AI coding agents in sandboxed Docker containers.
- Core tech: Python, Docker, Docker Compose, Typer, pytest, uv, ruff, ty.

Used (minimum) versions can be found in pyproject.toml.

## Layout

- `tests/`: unit tests for the agent-circus tool.
- `src/`: source code of this repository/agent-circus
- `pyproject.toml`: formal description of the project environment/dependencies

## Common Commands

Always prefix any python-related command with `uv run`, e.g.:

- Run tests: `uv run pytest`
  - Run test "name": `uv run pytest -k name`
  - Run test with marker "marker": `uv run pytest -m marker`
  - Run test in specific path "tests/file.py": `uv run pytest tests/file.py`
- Collect tests: `uv run pytest --co`
- Lint/format: `uv run ruff check`, `uv run ruff format --check`, `uv run ty check`

## Testing Patterns

- Naming: `test_<feature>_<scenario>.py`, test functions `test_<action>_<expected_outcome>`
- Create tests for utility functions such as parsing functions.

## Implementation Details

- Add concise/precise docstrings to utility functions.
  - Docstrings use Python Sphinx format (e.g. `:param foo:`, `:returns:`, `:raises ValueError:`).
- Add Python typing hints where possible.
- Prefer pragmatic solutions; if a shortcut is taken, call out the shortcomings explicitly.
