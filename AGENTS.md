# AGENTS.md

## Repository Overview

- Purpose: CLI tool for running AI coding agents in sandboxed Docker containers.
- Core tech: Python, Docker, Docker Compose, Typer, pytest, uv, ruff, ty.

Used (minimum) versions can be found in pyproject.toml.

## Layout

- `tests/`: unit tests for the agent-circus tool.
- `src/`: source code of this repository/agent-circus
- `src/agent_circus/templates`: dependencies and versions for containerized agents/libraries
- `pyproject.toml`: formal description of the project environment/dependencies

## Common Commands

Always prefix any python-related command with `uv run`, e.g.:

- Run tests: `uv run pytest`
  - Run test "name": `uv run pytest -k name`
  - Run test with marker "marker": `uv run pytest -m marker`
  - Run test in specific path "tests/file.py": `uv run pytest tests/file.py`
- Collect tests: `uv run pytest --co`
- Lint/format: `uv run ruff check`, `uv run ruff format --check`, `uv run ty check`

## Commit Messages

For agent template dependency update commits, use this format:

```text
chore: update agent template dependencies

Updated versions:

- <tool>: <old> -> <new>

Python dependency updates:

- <package>: <old> -> <new>

Signed-off-by: <name> <email>
```

- Omit a section if there are no entries for it.
- Use the package/tool names as they appear in the project where practical, preserving established capitalization from previous dependency update commits.
- Include transitive Python dependency changes from `uv.lock` under "Python dependency updates".

## Testing Patterns

- Naming: `test_<feature>_<scenario>.py`, test functions `test_<action>_<expected_outcome>`
- Create tests for utility functions such as parsing functions.
- You won't be able to run any docker-related commands because you are running inside a container and you don't have the docker socket mounted into it.

## Implementation Details

- Add concise/precise docstrings to utility functions.
  - Docstrings use Python Sphinx format (e.g. `:param foo:`, `:returns:`, `:raises ValueError:`).
- Add Python typing hints where possible.
- Prefer pragmatic solutions; if a shortcut is taken, call out the shortcomings explicitly.
