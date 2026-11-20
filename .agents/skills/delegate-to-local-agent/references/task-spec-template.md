# Task spec template

The agent only sees what you write here: it has no access to your
conversation. Write for a less capable reader: explicit, concrete, one step.
Give exact file paths, function signatures, data types, and an example
input/output where it helps. Paste the code the agent needs to see instead of
telling it to look around.

Write the spec to a file outside the repo so it never ends up in the diff,
then use this structure:

```markdown
# Task: <one-line goal>

## Context
<What the code does, relevant files and functions, conventions to follow. Paste the
key signatures and snippets. Don't assume the agent will find them.>

## Requirements
- <concrete, checkable requirement>

## Scope
- Files you may change: <paths/globs>
- Do NOT change: <paths>; do not add dependencies unless listed here: <list or "none">

## Done when
- `<test/build/lint command>` passes
- <other acceptance criteria>

## Known failures before your change
<Tests that already fail in this worktree. Leave them as they are.
Do not run git commands to check them; this list is the check.
Delete this section if the baseline was clean.>

## Rules
- Work only in the current directory. Do not commit, push, or touch git config.
- Do not run git commands that change the repository: no `stash`, `reset`,
  `checkout`, `switch`, `restore`, `rebase`, `merge`, `clean` or `branch`.
  Read-only commands such as `git status`, `git diff` and `git log` are fine.
- No network access beyond what the build needs. Never print or write secrets.
- Finish with a short summary: what you changed, what you're unsure about, anything left undone.
```

## Quality bar

Every requirement must be checkable by a command or by reading the diff. If a
requirement reads like a preference ("clean", "nice", "robust"), rewrite it as
a test or delete it.

The Scope section is the safety boundary for review: anything the agent
changes outside it gets flagged. Name the files the agent may not touch
explicitly, including lockfiles and CI configuration.

Keep the Rules section verbatim. It is what keeps the agent from committing,
pushing, or reaching the network.

## Review feedback rounds

When a run fails review, append a section to the same file and rerun:

```markdown
## Review feedback (round N)

- <file>:<line>: what is wrong, and what the correct result looks like.
```

Be specific enough that a tired reader could fix each point without asking a
question. Vague feedback ("handle edge cases better") does not land with a
small model.
