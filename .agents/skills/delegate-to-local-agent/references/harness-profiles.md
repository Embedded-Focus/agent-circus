# Harness profiles

Configuration and invocation details for the supported local coding-agent
harnesses. Read this on the first use of a profile in a session: these CLIs
change fast.

## Profile table

| `AGENT_HARNESS` | `AGENT_CMD` | Non-interactive invocation | `AGENT_INPUT` | `Project config` |
|---|---|---|---|---|
| `pi` | `pi` | `pi --approve -p "<instruction>" < task.md` (stdin is merged into the prompt) | `stdin` | `.pi/` |
| `omp` (oh-my-pi) | `omp` | Pi fork; assumed to match Pi's `-p` print mode | `stdin` | `.omp/`, `.pi/` (assumed, verify) |
| `opencode` | `opencode` | `opencode run "<instruction + spec>"` (message as positional argument) | `arg` | `.opencode/`, `opencode.json`, `opencode.jsonc` |
| `custom` | must be set | `AGENT_CMD`, `AGENT_ARGS` and `AGENT_INPUT` must all be set explicitly | must be set | `AGENT_PROJECT_CONFIG` |

On the first use of a profile in a session, run `<AGENT_CMD> --help` and
confirm the non-interactive flag and input handling still match this table.
If they don't match, stop and tell the user rather than guessing flags. A
harness that silently falls back to its interactive TUI does no work and may
hang.

The project config column lists only project-local paths, relative to the
repository root. The skill copies them into the worktree only when they
exist and git ignores them. User-level config in the home directory is
never copied: the harness finds it from any folder, including the worktree.

pi loads project-local files (`.pi/`) only from folders it trusts. A new
worktree path is not trusted, so the `pi` profile passes `--approve`, which
trusts the project files for this run only. The `omp` profile assumes the
same flag; check `omp --help` before you rely on it.

## Safety assumptions

Harnesses differ in what they allow by default in non-interactive mode (some
approve every tool call automatically). Assume the agent can run any command
the user account can. That's why `AGENT_CMD` should point to a sandbox
wrapper (for example a container launcher) rather than the bare binary.

## Adding a harness

A harness fits this skill if it has a non-interactive mode that takes a
prompt, edits files in the current directory, and exits. To add one, add a row
to the profile table: the command, the exact non-interactive invocation
(checked with `--help`), and how it receives the prompt (`stdin` or `arg`).
Note any default that matters for safety, such as auto-approving tool calls.
