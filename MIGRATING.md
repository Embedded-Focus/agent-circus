# Migrating from agent-circus 1.x to 2.x

agent-circus 2 runs agents in [Microsandbox](https://microsandbox.dev/)
micro-VMs instead of Docker or Podman containers. This guide moves a 1.x
project to 2.x. For the background, see the
[`v1` branch](https://github.com/Embedded-Focus/agent-circus/tree/v1)
and [ARCHITECTURE.md](ARCHITECTURE.md).

## Requirements

- A Linux host with KVM (`ls /dev/kvm` must show the device). msb needs
  KVM; agent-circus does not use it directly.
- Docker or Podman on the host only builds the sandbox image. It never
  runs agent workloads.
- No separate msb installation: the `microsandbox` Python package
  bundles the `msb` binary.

## Before the Upgrade

Remove the 1.x containers while 1.x is still installed:

``` shell
agent-circus rm
```

2.x manages sandboxes through msb, not through Docker Compose. It does
not see or remove the 1.x containers.

## Config Keys

1.x read a `.agent-circus/config.toml` file. 2.x reads the same file,
but only supports a subset of the keys. Unsupported 1.x keys produce a
warning and no effect; unknown or misplaced keys stop the command with
an error.

### Where Settings Live in 2.x

2.x reads these tables from `.agent-circus/config.toml`:

- `[msb]` with `binary`, and the sub-tables `[msb.sandbox]`,
  `[msb.network]`, `[msb.mounts]` (settings that map directly to the
  msb binary);
- the top-level tables `[image]`, `[agent]`, `[tracing]`, `[skills]`,
  `[[daemons]]`, `[hooks]`, `[env]`, `[logging]`, `[git]`, `[ssh]`,
  `[hosts]`, plus `env_passthrough`, `[[additional_dirs]]` and
  `[[port_forwards]]`.

The stricter rules:

- Unknown or misplaced keys now **fail the command** (they were warned
  about or ignored before).
- Under `[msb]`, only `binary`, `sandbox`, `network`, `mounts` are
  allowed.
- Unknown keys inside any table (`[image]`, `[git]`, `[ssh]`, ...) fail.
- `[skills]` may only be set in the user config
  (`~/.config/agent-circus/config.toml`), not in the project config.
- The user config and the project config merge key by key: tables
  merge recursively and the project value wins; arrays (for example
  `[[daemons]]` or `env_passthrough`) are replaced as a whole by the
  project value.

### Keys From 1.x

| 1.x key | 2.x replacement |
|---|---|
| `[hooks]` | `[hooks]` (see below) |
| `[ca_certs]` | `[msb.network] trust_host_cas = true` (see below) |
| `[env]` | `[env]` still works, but the behavior changes (see below) |

### `[hooks]` in 2.x

2.x also reads hooks from a top-level `[hooks]` table. It knows the
`provision` and `startup` hooks. The 1.x image-build hook names
`base_root` and `base_user` raise an error; agent-circus 2 has no
image build to run them in. 1.x hook scripts often assume the `node`
user and the 1.x image. Both are gone in 2.x: sandboxes run as root.

- 1.x `base_root` and `base_user` become `[hooks]` `provision`. It
  runs once at sandbox creation, not at image build. Everything runs
  as root, so `/home/node/...` paths become `/usr/local/bin` or
  `/root/...`.
- 1.x `startup` stays `[hooks]` `startup`.
- Hook files in `.agent-circus/hooks/` can stay where they are. Point
  `file` at them.
- For real image-level customization, use a custom `[image] dockerfile`
  based on the bundled `Dockerfile.msb-agents`.

See [Hooks](README.md#hooks) in the README.

### `[ca_certs]` to `[msb.network] trust_host_cas`

1.x selected host CA certificate files by filename pattern and
installed them in the container. 2.x cannot select single
certificates. Instead, it copies every CA that the host trusts into the
sandbox:

``` toml
[msb.network]
trust_host_cas = true
```

On Debian or Ubuntu hosts, put certificate files into
`/usr/local/share/ca-certificates/` and run `sudo update-ca-certificates`
on the host first. The option applies only when the sandbox is created;
re-create the sandbox after changing it
(`agent-circus rm -f && agent-circus up`). See
[CA Certificates](README.md#ca-certificates) in the README.

### `[env]` is applied when commands run

1.x wrote `[env]` into the image at build time, as `ENV` lines. 2.x does
not change the image; it passes the variables when it runs a command
(`exec`, hooks, daemons). A value such as
`PATH = "$PATH:/home/node/.local/bin"` appends to the sandbox's own
`PATH` at command time. A service of the image keeps the value that it
had when the sandbox was created; re-create the sandbox to change such
a value.

## Keys Without a Replacement

These 1.x keys have no 2.x equivalent. agent-circus 2 logs a warning
for them and ignores them.

- `[[mcp_servers]]`: MCP servers ran as sidecar containers, and 1.x
  injected the server URLs into every agent's configuration. 2.x runs
  one sandbox per workspace and has no sidecar concept. Run an MCP
  server inside the sandbox yourself, for example as a daemon under
  `[[daemons]]`.
- `[llama_cpp]`: ran a llama.cpp server as a sidecar for OpenCode. Run
  the server inside the sandbox instead (for example as a daemon).
- `[claude_mem]`: installed Claude-Mem in the image. Install it in the
  sandbox (for example in a `provision` hook).
- `[[data_stores]]`: persisted per-project directories under
  `~/.local/state/agent-circus/<project>/data/`. 2.x keeps all state in
  the sandbox and its disks; use the workspace mount at `/workspace`.
- `[host_credentials]`: mounted an agent's host authentication file
  read-write. 2.x bind-mounts the host config directories of Claude
  Code and Codex into the sandbox directly (see
  [Running Agents](README.md#running-agents)).
- Deploy mode (`agent-circus init --deploy`): copied templates into
  `.agent-circus/` for per-project customization. 2.x has no deploy
  mode; the image is built from the bundled
  `Dockerfile.msb-agents`. 2.x ignores a leftover
  `.agent-circus/compose.yaml` and logs a warning.

## Commands That 2.x Does Not Have

- `init`: wrote a starter config and (with `--deploy`) deployed
  templates. Write `.agent-circus/config.toml` by hand; see the
  [Configuration Reference](README.md#configuration-reference).
- `completion`: installed shell completions. Not needed anymore.
- `config`: managed a key/value config store. 2.x reads plain
  `config.toml` files.
- `deps`: pinned and updated container dependency snapshots. 2.x pins
  the agent harness versions in the image; update them with
  `agent-circus-update-templates` when you rebuild the image.

## Staying on 1.x

Pin the major version to keep 1.x:

``` shell
uv tool install 'agent-circus<2'
```

To run 1.x once without installing it:

``` shell
uvx --from 'agent-circus<2' agent-circus version
```

uv installs one version of a package as a tool, so 1.x and 2.x cannot
be installed as tools at the same time.
