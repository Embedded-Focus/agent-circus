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

Read this section before running `git commit`.

### General rules

- Group changes by feature. One commit carries one feature or one fix.
- Never mix unrelated changes in one commit. Stage only the files that
  belong to the change; leave unrelated files unstaged.
- Never commit generated files, editor state, or local configuration.
  Check `.gitignore` first. If a local artifact is not ignored, ask
  before adding a new ignore pattern.
- Write the subject line in the imperative mood. Say what the commit
  does: "Add a debug panel", not "Added" or "Adds".
- Keep the subject line under 60 characters (excluding the Conventional
  Commit prefix, see below). Do not end it with a period.
- Add exactly one paragraph of description below the subject, separated
  by one blank line. Keep it short; three sentences are enough for most
  changes.
- Write the message in simple English. Use short sentences, active
  voice, and simple tenses. Name the actor. Do not use contractions.
- Say why the change is needed, then what it does. Do not list every
  file. Do not describe the code line by line.
- Do not add AI credits, tool names, or "Generated with" footers.

This repository uses Conventional Commit prefixes on the subject line
(`feat:`, `fix:`, `refactor:`, `test:`, `chore:`, ...), with an optional
scope such as `(msb-circus)` or `(agent-circus)`. Follow the established
style of recent commits.

Message shape (adapt the words, keep the shape):

- New feature:

  ```text
  feat: add <feature>

  <User needs this because ...>. Add <what you built>. <One sentence
  about how it behaves or how it is enabled.>
  ```

- Bug fix:

  ```text
  fix: <what was broken>

  <What happened before, in one sentence.>. <What the cause was.>.
  <What the code does now.>.
  ```

- Dependency or tooling change:

  ```text
  chore: <what was updated>

  <What failed or was outdated.>. <What you changed.>. <One sentence
  that says the checks still pass.>
  ```

### Before you commit

- Run the checks and catch failures early:

  ```bash
  uv run pytest
  uv run ruff check
  uv run ruff format --check
  uv run ty check
  ```

- If a hook fails, fix the problem and commit again. Do not use
  `--no-verify` unless the user asks for it.
- Do not sign commits. Run `git commit` with `--no-gpg-sign`, and do not
  use `-S`.
- Show the user the commit hash and subject after a successful commit.

### Agent template dependency updates

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
- The `Signed-off-by:` trailer is a DCO statement, not a GPG signature;
  it is compatible with `--no-gpg-sign`.

## Testing Patterns

- Naming: `test_<feature>_<scenario>.py`, test functions `test_<action>_<expected_outcome>`
- Create tests for utility functions such as parsing functions.
- You won't be able to run any docker-related commands because you are running inside a container and you don't have the docker socket mounted into it.

## Implementation Details

- Add concise/precise docstrings to utility functions.
  - Docstrings use Python Sphinx format (e.g. `:param foo:`, `:returns:`, `:raises ValueError:`).
- Add Python typing hints where possible.
- Prefer pragmatic solutions; if a shortcut is taken, call out the shortcomings explicitly.
