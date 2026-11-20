# Settings Migration Cheat Sheet (agent-circus 2)

Quick reference for config changes in this release. Full guide:
[MIGRATING.md](MIGRATING.md).

## Moved tables

| Old (error now) | New |
|---|---|
| `msb.binary` | stays |
| `[msb.sandbox]` | stays |
| `[msb.network]` | stays |
| `[msb.mounts]` | stays |
| `[msb.image]` | `[image]` |
| `[msb.agent]` | `[agent]` |
| `[msb.tracing]` | `[tracing]` |
| `[msb.skills]` | `[skills]` (user config only) |
| `[[msb.daemons]]` | `[[daemons]]` |
| `[msb.hooks]` | `[hooks]` |
| `[msb.env]` | removed — use `[env]` |

The fields inside the tables do not change; only the table name moves.

## Stricter rules

- Unknown or misplaced keys now **fail the command** (they were warned
  about or ignored before).
- Under `[msb]`, only `binary`, `sandbox`, `network`, `mounts` are
  allowed.
- Unknown keys inside any table (`[image]`, `[git]`, `[ssh]`, ...) fail.
- 1.x top-level keys (`[mcp_servers]`, `[llama_cpp]`, `[claude_mem]`,
  `[[data_stores]]`, `[host_credentials]`, `[ca_certs]`) still only
  warn, pointing to `MIGRATING.md`.
- `[hooks] base_root` / `base_user` (1.x image-build hooks) fail: use
  `[hooks] provision` instead.
- `[skills]` may only be set in the user config
  (`~/.config/agent-circus/config.toml`), not in the project config.
