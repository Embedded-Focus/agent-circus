# msb-circus

*Run AI coding agents in Microsandbox micro-VMs with one configuration file.*

[Getting Started](#getting-started) ·
[How It Works](#how-it-works) ·
[Running Agents](#running-agents) ·
[Editors](#setting-up-editors-to-work-with-acp) ·
[Images](#images) ·
[Lifecycle](#lifecycle) ·
[Configuration](#configuration) ·
[Secrets](#secrets) ·
[Networking](#networking) ·
[Host Entries](#host-entries) ·
[CA Certificates](#ca-certificates) ·
[Troubleshooting](#troubleshooting)

## Overview

msb-circus runs AI coding agents inside [Microsandbox](https://microsandbox.dev/)
(msb). Microsandbox starts each sandbox as a small virtual machine on your
Linux host. Each sandbox boots from an OCI image, the same image format that
Docker and Podman use.

The sandbox image contains the agent harnesses and a Docker engine. An agent
harness is the CLI tool that drives an agent, for example `pi` or `opencode`.
The Docker engine runs inside the sandbox, so agents can build and run
containers in their own workspace. The Docker or Podman installation on your
host only builds the sandbox image. It never runs agent workloads.

msb-circus reads the same `.agent-circus/config.toml` file as
[agent-circus](README.md). It applies the keys it supports and logs a warning
for every other key. You can use both tools on the same project.

The supported agent harnesses come preinstalled in the image:

- [Pi](https://pi.dev/) (Earendil Works), the default interactive harness
- [OpenCode](https://opencode.ai/) (SST)
- [Claude Code](https://github.com/anthropics/claude-code) (Anthropic)
- [Codex](https://github.com/openai/codex) (OpenAI)
- [pi-acp](https://github.com/svkozak/pi-acp), the ACP adapter for Pi
- [claude-agent-acp](https://github.com/agentclientprotocol/claude-agent-acp),
  the ACP adapter for Claude Code
- [codex-acp](https://github.com/agentclientprotocol/codex-acp), the ACP
  adapter for Codex

Editors and IDEs connect to the agents through the
[Agent Client Protocol](https://agentclientprotocol.com/) (ACP). In ACP mode,
the editor talks to the agent through the sandbox, and the sandbox boundary
stays in place.

## Getting Started

This section gets you from zero to a first agent session. Read the
requirements first, because KVM and an image builder are host features.

### Requirements

You need a Linux host with KVM. KVM is the virtual machine feature of the
Linux kernel. msb-circus does not need it directly, but msb does. Run
`ls /dev/kvm` to make sure that KVM is available. You also need
[uv](https://docs.astral.sh/uv/getting-started/installation/)
and one image builder, Podman or Docker, on the host.

You do not need a separate msb installation. The `microsandbox` Python
package bundles the `msb` binary, and msb-circus finds it there.

### Installing msb-circus

msb-circus ships in this repository together with agent-circus. To install
both tools from a checkout, run this command in the repository root:

``` shell
uv tool install .
```

To run msb-circus without installing it, use `uv run` from the repository
root:

``` shell
uv run msb-circus version
```

### Version Information

The `version` command prints the version of msb-circus, the path of the msb
binary, and the msb version. msb-circus expects the msb version that matches
the installed `microsandbox` package. It logs a warning when a msb binary on
your `PATH` differs from that version, because command line flags can
change between releases.

## How It Works

msb-circus manages one sandbox per workspace. The sandbox name is
`agents-<project>`, where `<project>` is the name of your project directory.
All agent harnesses run in this one sandbox. You select the harness per
command, not per sandbox.

The lifecycle has three steps:

1. Build the agent image on the host and import it into the msb image cache.
2. Boot the sandbox from that image with `msb-circus up`.
3. Start a harness with `msb-circus exec` whenever you need one.

The sandbox boots with `msb run --detach`. This starts the image entrypoint,
which starts the Docker engine in the guest and imports the TLS interception
certificate before that. A `msb create` boot skips the entrypoint.
msb-circus never uses it.

After a boot or a start, msb-circus asks the guest for `docker info` until
the Docker engine answers. If the engine does not answer within the probe
budget, msb-circus prints the last probe error and the path to the guest log.

## Images

The `build` command builds the agent image on the host and imports it into
the msb image cache:

``` shell
msb-circus build --platform linux/amd64
```

The image comes from a Dockerfile that ships with msb-circus. That
Dockerfile pins the base image, the uv image, and all agent versions by
hash, so builds are reproducible. `build` writes the image to a temporary
tar file, imports it with `msb load`, and deletes the tar file. Use
`--keep-tar <path>` to write the tar file to a path and keep it.

The build skips itself when the image tag is already in the msb image cache.
Use `--force` to rebuild anyway. Use `--engine docker` to build with Docker
instead of Podman.

The `import` command loads an existing docker-archive tar file into the msb
image cache:

``` shell
msb-circus import msb-agents.tar
```

## Lifecycle

All commands work from your project directory and address the sandbox
`agents-<project>` of that workspace. The commands are:

| Command | Effect |
| --- | --- |
| `msb-circus up` | Boot the sandbox, or reuse the running one. |
| `msb-circus ps` | Show sandbox status for this workspace. |
| `msb-circus exec ...` | Run a command or an ACP session in the sandbox. |
| `msb-circus stop` | Stop the sandbox. Guest state persists. |
| `msb-circus start` | Start the sandbox again and verify the Docker engine. |
| `msb-circus rm -f` | Remove the sandbox and its guest state. |
| `msb-circus destroy` | Remove the sandbox with a confirmation prompt. |

`up` never replaces an existing sandbox. Guest state survives every `up`.
Your workspace files come from the host and stay on the host, so `rm` and
`destroy` do not touch them. To reclaim the space of cached images, run
`msb image prune`.

`ps` prints a `docker compose ps`-style table (`NAME`, `ID`, `CREATED`,
`STATUS`) with relative creation ages. `ps --all` lists every sandbox
that msb knows about, not only the sandbox of this workspace.
`ps --running` lists only running sandboxes.

For scripts, `ps --format json` prints a JSON array with `name`, `id`,
`status`, `created_at` and `updated_at` per sandbox, and `ps --quiet`
prints sandbox names only. Both stay silent when no sandbox matches.

## Running Agents

Boot the sandbox once, then start a harness:

``` shell
msb-circus up
msb-circus exec
```

Without a command, `exec` starts Pi, the default harness. To run another
command, pass it after `--`:

``` shell
msb-circus exec -- bash
msb-circus exec -- docker ps
msb-circus exec -- opencode
msb-circus exec -- claude
msb-circus exec -- codex
```

`exec` boots the sandbox first when it is not running. The workspace is
mounted at the same absolute path as on the host, so file paths agree
between your editor and the agent.

msb-circus brings your host harness configuration into the sandbox:

- **Claude Code and Codex**: the host config directories `~/.claude` and
  `~/.codex` are bind-mounted into the guest (`/root/.claude` and
  `/root/.codex`) when the sandbox is created. The mount is read-write and
  carries the credentials file (`.credentials.json`, `auth.json`), so a
  login or token refresh inside the sandbox persists on the host, and the
  other way around. A missing host directory is skipped with a warning;
  create it and re-create the sandbox (`msb-circus rm -f && msb-circus up`)
  to mount it. Because msb fixes mounts at creation, msb-circus also warns
  when a running sandbox was created without a mount that is expected now.
- **Pi and OpenCode**: selected config files (`~/.pi/agent/settings.json`,
  `~/.pi/agent/models.json`, `~/.config/opencode/opencode.json`) are copied
  into the guest after every boot or start. The copy is one-way: guest-side
  edits to these files are overwritten by the next refresh. Authenticate
  through `env_passthrough` (see [Secrets](#secrets)).

msb-circus pre-trusts the agent workdir (`[msb.agent] workdir`) and every
`additional_dirs` mount for pi and pi-acp: after each boot or start it
writes `~/.pi/agent/trust.json` in the guest, so the first harness start in
a new sandbox does not ask to trust the directory. The write is idempotent
and **overwrites** an existing trust file — add your own entries through
the configuration, not by editing the file in the guest. If the write
fails, msb-circus warns and pi simply asks on first start, as before.

### ACP Sessions

For an ACP session from an editor or IDE, add `--acp`:

``` shell
msb-circus exec --acp -- pi-acp
msb-circus exec --acp -- opencode acp
msb-circus exec --acp -- claude-agent-acp
msb-circus exec --acp -- codex-acp
```

In ACP mode, msb-circus streams the session between your editor and the
sandbox without a pseudo-terminal. Standard output carries only ACP
messages. msb-circus keeps standard error silent: editors treat any
stderr output from the agent as an API error and show an error dialog
for it. Warnings and status messages therefore go to the log file (see
[Configuration](#configuration) below); pass `--log-file` or configure
`[logging]` to keep them, otherwise `/tmp/msb-circus-acp.log` is used. Fatal errors
still print to standard error right before msb-circus exits. The stream
copies bytes without change, so large messages and non-ASCII text
arrive intact. When the editor disconnects, msb-circus stops the agent
process in the sandbox.

## Setting up Editors to Work with ACP

### Emacs

This is a working [agent-shell](https://github.com/xenodium/agent-shell)
configuration based on msb-circus. The runner starts the harness through
`msb-circus exec --acp`, so the editor talks to the sandbox over ACP. The
path resolver maps file paths between your editor and the `/workspace`
mount in the sandbox.

The sandbox carries Pi, OpenCode, Claude Code, and Codex. An identifier
without a harness in the sandbox falls back to Pi through the pi-acp
adapter. API keys do not go into
this configuration; msb-circus reads them from `env_passthrough` on the
host. If you change the mount in `[msb.mounts]`, change the workspace path
constant to match it.

``` emacs-lisp
(defconst rpo/agent-shell--msb-workspace-path "/workspace/"
  "The workspace path inside the msb-circus sandbox.")

(defun rpo/agent-shell--resolve-msb-path (path)
  "Resolve PATH between local filesystem and the sandbox workspace.

For example:

- /workspace/README.md
    => /home/you/projects/my-project/README.md
- /home/you/projects/my-project/README.md
    => /workspace/README.md"
  (let ((cwd (agent-shell-cwd)))
    (if (string-prefix-p cwd path)
        ;; Local -> sandbox
        (string-replace cwd rpo/agent-shell--msb-workspace-path path)
      ;; Sandbox -> local
      (if agent-shell-text-file-capabilities
          (if-let* ((_ (string-prefix-p rpo/agent-shell--msb-workspace-path path))
                    (local-path (expand-file-name
                                 (string-replace rpo/agent-shell--msb-workspace-path cwd path))))
              (or
               (and (file-in-directory-p local-path cwd) local-path)
               (error "Resolves to path outside of working directory: %s" path))
            (error "Unexpected path outside of workspace folder: %s" path))
        (error "Refuse to resolve to local filesystem with text file capabilities disabled: %s" path)))))

(defun rpo/agent-shell-msb-runner (buffer)
  "Return the msb-circus exec command prefix to run for BUFFER's agent.

Looks up the agent identifier in BUFFER's `agent-shell' config and
selects the matching harness in the sandbox. The sandbox carries
Pi, OpenCode, Claude Code, and Codex; every other identifier falls
back to Pi through the pi-acp adapter."
  (let* ((cfg (agent-shell-get-config buffer))
         (id  (map-elt cfg :identifier))
         (harness
          (pcase id
            ('opencode '("opencode" "acp"))
            ('claude-code '("claude-agent-acp"))
            ('codex '("codex-acp"))
            (_ '("pi-acp")))))
    (append (list "msb-circus" "exec" "--acp" "--") harness)))

(use-package agent-shell
  :ensure t
  :config
  (setq acp-logging-enabled t)
  (setq agent-shell-command-prefix #'rpo/agent-shell-msb-runner)
  (setq agent-shell-path-resolver-function #'rpo/agent-shell--resolve-msb-path)
  (setq agent-shell-file-completion-enabled t))
```

Start the sandbox before the first session, or keep it running. `exec`
boots the sandbox by itself, but the first session then waits for the boot.

``` shell
msb-circus up
```

## Configuration

msb-circus reads `.agent-circus/config.toml` in your project, with the same
layering as agent-circus: a user-global file from
`$XDG_CONFIG_HOME/agent-circus/config.toml`, then the project file. The
project file wins.

msb-circus applies its own `[msb]` table, plus the `logging`,
`env_passthrough`, `additional_dirs`, `git` (only `worktree_mirror`), and
`port_forwards` keys of agent-circus. Every other key produces a warning.
agent-circus in turn logs a warning for the unknown `[msb]` table and
continues.

msb-circus logs to stderr. Set the level and an optional log file either on
the command line or in the config; the command line wins.

``` shell
msb-circus --log-level DEBUG --log-file /tmp/msb-circus.log exec --acp --
```

``` toml
[logging]
level = "DEBUG"
file = "/tmp/msb-circus.log"
```

In ACP mode (`msb-circus exec --acp`), msb-circus never writes logs to
stderr — editors report stderr output from the agent process as API
errors (see [ACP Sessions](#acp-sessions)). Logs then go to the log file
only; when no log file is configured, `/tmp/msb-circus-acp.log` is
used.

## Execution Traces

An execution trace records resource use and ACP traffic metadata for one
agent session. The trace does not change the ACP byte stream. For terminal
sessions, it also does not intercept the terminal input or output.

Use `--trace` to enable a trace for one session:

``` shell
msb-circus exec --trace -- pi
msb-circus exec --trace --acp -- pi-acp
```

By default, msb-circus writes each trace to a new directory under
`.agent-circus/traces/`. The `latest` file contains the ID of the newest
trace. The project `.gitignore` excludes the `.agent-circus` directory.
msb-circus does not delete old traces. Remove old trace directories when you
no longer need them.

Use this configuration to enable traces for all `exec` commands:

``` toml
[msb.tracing]
enabled = true
directory = ".agent-circus/traces"
sample_interval = 1.0
repetition_threshold = 64
capture_payloads = false
max_payload_bytes = 104857600
```

The `sample_interval` value sets the time in seconds between resource
samples. The `repetition_threshold` value sets the number of equal bytes that
creates an event. Relative values for `directory` use the project workspace
as their base.

Command-line options take priority over the configuration:

``` shell
msb-circus exec --trace --trace-sample-interval 0.5 -- pi
msb-circus exec --no-trace -- pi
msb-circus exec --trace --trace-dir /tmp/msb-traces -- pi
```

### Trace Files

Each trace directory contains files for one execution. The trace directory
uses mode `0700`. Trace files use mode `0600` because some files can contain
project or session data.

The main files are:

| File | Contents |
| --- | --- |
| `manifest.json` | Trace version, start time, sandbox resources, executable name, and environment variable names. |
| `summary.json` | Exit status, duration, maximum VM use, stream counts, and repetition event count. |
| `events.jsonl` | Trace lifecycle events, collector errors, and repeated-byte events. |
| `vm-metrics.jsonl` | VM CPU, memory, disk, and network samples from `msb metrics`. |
| `processes.jsonl` | Guest process CPU, memory, I/O, thread, and file descriptor samples from `/proc`. |
| `acp-transport.jsonl` | ACP chunk direction, size, hash, newline count, and longest equal-byte run. |
| `msb-system.jsonl` | Microsandbox lifecycle, runtime, and kernel log entries. |
| `msb-circus.log` | Log messages from msb-circus during the execution. |
| `collector-errors.log` | Error output from trace collector processes. |

The process trace does not save command arguments or environment values. The
manifest saves only the executable name. These limits prevent common prompts
and API keys from entering the normal trace files.

`acp-transport.jsonl` contains data only for ACP sessions. A terminal trace
still contains VM metrics and guest process samples. Microsandbox records only
the first primary exec session in its execution log, so this trace does not
use that log as a copy of the agent stream.

### Raw ACP Payloads

Use `--trace-payloads` when chunk metadata is not sufficient. This option also
enables tracing. It saves the exact ACP input, output, and error streams under
the `payloads/` directory.

``` shell
msb-circus exec --acp --trace-payloads -- pi-acp
```

Raw payloads can contain prompts, source code, tool results, and credentials.
Do not share these files without inspection. The `max_payload_bytes` value
limits each stream, and metadata collection continues after this limit.

### Evaluate a Trace

Read `summary.json` first. It shows whether the agent exited normally and
whether the repetition detector found an incident. It also shows the maximum
VM CPU and memory values that were available.

``` shell
trace_id=$(cat .agent-circus/traces/latest)
trace_dir=".agent-circus/traces/$trace_id"
jq . "$trace_dir/summary.json"
```

Read all repeated-byte events with this command:

``` shell
jq 'select(.event == "repetition_detected")' "$trace_dir/events.jsonl"
```

Byte `33` is the ASCII value for `!`. If an event contains byte `33`, use its
time and sequence values to find the related ACP chunk:

``` shell
jq 'select(.longest_repeated_byte.count >= 64)' \
  "$trace_dir/acp-transport.jsonl"
```

Use the VM metrics to compare CPU and memory at the incident time:

``` shell
jq '{timestamp, cpu_percent, memory_bytes, memory_limit_bytes, net_rx_bytes, net_tx_bytes}' \
  "$trace_dir/vm-metrics.jsonl"
```

Use the process samples to find the processes that gained the most CPU ticks
during the trace. One CPU tick is a kernel accounting unit. The sample file
records the kernel tick rate. The process tick values are cumulative.

``` shell
jq -s '
  [.[].processes[]]
  | group_by(.pid)
  | map(
      select(length > 1)
      | {
          pid: .[0].pid,
          name: .[0].name,
          cpu_ticks: (
            (.[-1].user_ticks + .[-1].system_ticks)
            - (.[0].user_ticks + .[0].system_ticks)
          ),
          max_resident_pages: (map(.resident_pages) | max)
        }
    )
  | sort_by(.cpu_ticks)
  | reverse
  | .[:10]
' "$trace_dir/processes.jsonl"
```

If raw payload capture was enabled, search the agent output for a long run of
exclamation marks:

``` shell
rg -a -n '!{64}' "$trace_dir/payloads/agent-to-client.bin"
```

If the raw ACP output contains the run, the agent harness sent it to the ACP
client. If the VM CPU also rises, use `processes.jsonl` to identify the busy
guest process. If only VM CPU rises, inspect `msb-system.jsonl` and
`collector-errors.log` for a runtime or collector failure.

## Configuration Reference

The `[[additional_dirs]]` array mounts extra host directories into the
sandbox under `/workspaces/<name>`, next to the project at `/workspace`.
The fields match agent-circus: `path` is required, `readonly` defaults to
false, and `name` defaults to the basename of `path`.

``` toml
[[additional_dirs]]
path = "/home/user/shared-libs"
readonly = true

[[additional_dirs]]
path = "/home/user/other-project"
# name defaults to the basename of path
name = "other-project"
```

``` toml
env_passthrough = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]

[msb]
# binary = "/usr/local/bin/msb"        # default: the bundled msb binary

[msb.image]
tag = "docker.io/library/msb-agents:26"
# dockerfile = "Dockerfile.msb-agents" # default: the bundled Dockerfile
# platform = "linux/arm64"             # default: the host architecture

[msb.sandbox]
memory = "4G"
# cpus = 2                              # default: 1
# root_disk = "flat:32G"                # root disk spec, see "Docker and Disks"
# docker_disk = "10G"                   # docker opt-in: dedicated ext4 disk
                                        # for /var/lib/docker; unset = no docker

[msb.mounts]
"." = "/workspace"                     # default; more mounts: "/data" = "/data:ro"

[msb.network]
mode = "public,private"                # default; unrestricted network access
# trust_host_cas = false              # trust the host's CAs; see
                                       # "CA Certificates"

[[port_forwards]]                       # publish a guest port on the host
guest_port = 8000                       # or container_port (agent-circus style)
# host_port = 8000                      # default: guest_port
# host = "127.0.0.1"                    # default: loopback only
# protocol = "tcp"                      # or "udp"

[msb.agent]
command = ["pi"]                       # default exec target
workdir = "/workspace"

[ssh]                                  # forward the host SSH agent; see
                                       # "SSH Agent Forwarding"
# config_path = "~/.ssh/config"        # copied to /root/.ssh/config
# known_hosts_path = "~/.ssh/known_hosts"  # copied to /root/.ssh/known_hosts

[hosts]                                # forward host hosts-file entries; see
                                       # "Host Entries"
patterns = []                          # globs or re:-prefixed regexes
# file = "/etc/hosts"                  # hosts file to read
```

All fields are optional. The values above are the defaults.

## Secrets

API keys never go into the image. msb-circus passes them per command with
`--env KEY=VALUE`, or you list them once under `env_passthrough`:

``` shell
msb-circus exec -e OPENAI_API_KEY=sk-... -- pi
```

Claude Code and Codex usually need no API key variable: their credentials
live in the host config directories that msb-circus bind-mounts into the
sandbox (see [Running Agents](#running-agents)). `env_passthrough` is the
way to authenticate Pi and OpenCode, or to override the mounted
credentials for a single command.

msb-circus reads the variables under `env_passthrough` from your host shell
at every command. Entries are patterns, like in agent-circus: a plain name
must exist, or the command fails before msb starts. A glob (`LG_*`) or a
regex with the `re:` prefix (`re:^LG_`, matched case-insensitively) expands
to every matching variable that is set. Log output never contains secret
values.

## Networking

By default, a sandbox starts with `--net "public,private"`. This gives the
sandbox unrestricted network access, which the Docker engine in the guest
needs to pull images. Your model API endpoints and MCP servers are reachable
through this profile.

msb-circus does not yet translate a network policy into msb network rules.
Until that feature exists, treat the default as an open profile and keep
sensitive host services outside the reach of the sandbox.

### Port Forwarding

Use the `[[port_forwards]]` array to publish a guest port on the host, for
example to reach a dev server, a debugger, or an MCP server that the agent
starts inside the sandbox:

``` toml
[[port_forwards]]
container_port = 3333
# host_port = 3333      # default: container_port
# host = "127.0.0.1"    # default: loopback only
# protocol = "tcp"      # or "udp"
```

The fields match [agent-circus](README.md), so the same table works in both
tools. The `service` field of agent-circus entries is accepted but ignored:
msb-circus runs one sandbox per workspace, so there is nothing to select a
service from. `guest_port` is accepted as an alias for `container_port`.

Forwarded ports bind to `127.0.0.1` on the host by default — the same
default msb itself uses. Set `host = "0.0.0.0"` only when the service should
be reachable from other machines on your network. The process inside the
sandbox must bind to `0.0.0.0` or the guest network interface; a server
bound to `127.0.0.1` inside the guest is not reachable through the forward.
Servers differ in how they opt in, for example:

``` shell
opencode serve --hostname 0.0.0.0 --port 4096
```

Port mappings are fixed when the sandbox is created. `msb start` and `msb
modify` cannot change them. When `up` reuses an existing sandbox whose
mappings differ from the configuration, msb-circus prints a warning. Remove
and re-create the sandbox to apply the new ports:

``` shell
msb-circus rm -f
msb-circus up
```

If a forwarded port does not answer from the host with `Empty reply from
server` (or hangs), the service inside the sandbox most likely bound to the
guest loopback interface (`127.0.0.1`) instead of all interfaces. The
forward targets the guest network interface, so it cannot reach a
loopback-bound server. Restart the service bound to `0.0.0.0` in the guest,
for example `opencode serve --hostname 0.0.0.0 --port <port>`. If that does
not help, check whether another process
on the host holds the port. msb-circus probes every configured host port
before booting and fails with the conflicting `host:port -> guest/protocol`
mapping when it is already bound (for example by a second VM); the probe is
best-effort, so msb may still report a conflict grabbed between probe and
boot.

## Docker and Disks

The sandbox image ships Docker 29. Docker 29 stores images and containers
with the overlayfs snapshotter, and that snapshotter needs a real
filesystem under `/var/lib/docker`. The sandbox root filesystem is an
overlayfs by default, and the kernel refuses to stack one overlayfs on
another. A sandbox without disk configuration therefore cannot run
containers. The sandbox boots, and every `docker` command fails with
"Cannot connect to the Docker daemon". The boot log explains why.

Enable docker with one of two settings under `[msb.sandbox]`:

``` toml
[msb.sandbox]
# Option 1: a dedicated ext4 disk for docker data, removed with the
# sandbox. Docker data does not count against the root disk size.
docker_disk = "10G"

# Option 2: a flat root. The whole root filesystem becomes one real
# filesystem with no overlayfs, and docker data shares it with the rest
# of the guest. Size it for image content plus docker data (about 3G of
# image content plus your docker usage).
root_disk = "flat:32G"
```

Both settings can be combined: `root_disk = "flat:32G"` with
`docker_disk = "10G"` puts docker data on its own disk inside a flat
root. A plain size such as `root_disk = "16G"` keeps the managed root
and does not enable docker.

Without `root_disk`, the managed root stays at the msb default of 4G.
Docker data does not count against it, but image content and rootfs
writes do. The sandbox image carries about 3G of content, so expect
roughly 1G of headroom. If your agents write a lot outside
`/var/lib/docker`, raise the root next to the docker disk.

Disk settings apply at sandbox creation. msb-circus warns when the
settings of an existing sandbox differ from the configuration. Remove and
re-create the sandbox to apply them:

``` shell
msb-circus rm && msb-circus up
```

Removing the sandbox deletes the docker data disk, together with all
images and containers in it. The disk survives `stop` and `start`.

Host storage lives under the msb storage root (`~/.microsandbox` by
default). A flat root or a docker disk adds its size there per sandbox.

## Git Worktrees

Set `git.worktree_mirror = true` when the repository is also used outside
the sandbox and you need transparent Git worktree support:

``` toml
[git]
worktree_mirror = true
```

This adds a second bind mount for the workspace at its exact host absolute
path inside the sandbox (in addition to the standard `/workspace` mount).
Both paths point to the same directory, so Git's recorded absolute paths
are valid on both sides of the sandbox boundary — `git worktree list`,
`git worktree add`, and all other operations that consume or emit absolute
paths work without any manual path rewriting.

The mount applies at sandbox creation, like all msb mounts. msb-circus
warns when an existing sandbox was created without the mirror. Remove and
re-create the sandbox to apply the flag:

``` shell
msb-circus rm && msb-circus up
```

For best results, tell your agent to prefer the host path over
`/workspace` for Git operations — Git records the paths it is invoked
with, so a worktree created under `/workspace` may record paths that only
exist inside the sandbox. Paste this into your `AGENTS.md` (adjust the
path to your repository location):

``` markdown
## Git Operations

This repository is mounted at `/home/user/myrepo` inside the agent
sandbox in addition to the standard `/workspace` mount. When performing
Git operations that involve absolute paths — such as `git worktree`
commands — use `/home/user/myrepo` rather than `/workspace` to ensure
Git's recorded paths remain consistent with the host.
```

The remaining agent-circus `[git]` keys (`config_path`,
`signing_key_path`, ...) describe Docker-specific mounts and are not
supported; msb-circus warns about them and ignores them.

## SSH Agent Forwarding

An SSH agent is a small program on your host that holds your private SSH
keys and signs requests for them. With agent forwarding, the `ssh` and
`git` commands inside the sandbox use this agent. Git over SSH then works
without a private key in the sandbox, and no private key material ever
enters the guest.

Add an `[ssh]` table to `.agent-circus/config.toml` to turn the forwarding
on. An empty table is enough:

``` toml
[ssh]
```

The host needs a running SSH agent. Make sure that `SSH_AUTH_SOCK` is set
and that `ssh-add -l` on the host lists your keys. When `SSH_AUTH_SOCK` is
not set, `up` and `start` fail with an error before msb runs.

You can copy an SSH client configuration and a `known_hosts` file into the
sandbox:

``` toml
[ssh]
config_path = "~/.ssh/config"
known_hosts_path = "~/.ssh/known_hosts"
```

msb-circus copies these files to `/root/.ssh` after every `up` and
`start`. The copy is one-way. Files that you edit inside the sandbox are
overwritten at the next refresh.

A host `~/.ssh/config` with macOS-only options such as `UseKeychain`
makes `ssh` inside the sandbox fail. Add this line at the top of the file:

``` ssh-config
IgnoreUnknown UseKeychain
```

`IdentityFile` entries that point at host paths produce warnings only.
The keys come from the agent, not from files in the sandbox.

Two setup steps apply:

1. Rebuild the image once with `msb-circus build`. The image then contains
   `openssh-client` and `socat`.
2. Re-create the sandbox when you add or remove the `[ssh]` table:
   `msb-circus rm -f && msb-circus up`. The connection route between the
   sandbox and the host agent is fixed at sandbox creation.

After every boot and start, msb-circus refreshes the route target and runs
`ssh-add -l` inside the sandbox. The check warns when it cannot reach the
agent, for example after you restarted the agent on the host. Run
`msb-circus up` again in that case, so msb-circus refreshes the route.

Warning: every process in the sandbox can ask the host agent to sign. Run
`ssh-add -c` on the host to confirm each use, or run a dedicated agent
with only the keys that the sandbox needs.

## Host Entries

The hosts file of your host (`/etc/hosts` by default) maps names to IP
addresses. This is where a company VPN or a local test server adds its
names. The sandbox does not see these names, because it has its own hosts
file and its own resolver.

The `[hosts]` table forwards selected entries from the host hosts file
into the sandbox. The table uses the same pattern syntax as agent-circus:

``` toml
[hosts]
patterns = ["*.corp.internal", "myserver", "re:\\.vpn\\.example\\.com$"]
# file = "/etc/hosts"   # optional, defaults to /etc/hosts
```

A pattern matches a name in the hosts file. A pattern without `re:` is a
glob, where `*` matches any text. A pattern with `re:` is a regular
expression. Matching ignores upper and lower case. When one name of a
line matches, all names of that line are forwarded.

msb-circus writes the matching entries into a managed block in
`/etc/hosts` inside the sandbox. Everything outside this block stays as
it is. The block is refreshed at every `up`, `start`, and `exec`, so a
change on the host applies without re-creating the sandbox. Names resolve
through the guest resolver and never reach the DNS rules of msb.

Entries that point at a loopback address (`127.0.0.0/8` or `::1`) resolve
to the sandbox itself, not to your host. msb-circus warns about them.
Use `host.microsandbox.internal` (with the `host` network profile) to
reach services on your host instead.

Containers that an agent starts with the Docker engine inside the sandbox
do not get these entries.

If you remove the `[hosts]` table, the last block stays in the sandbox
until you re-create it. To empty the block without re-creating, set
`patterns = []` and run `msb-circus up` once.

## CA Certificates

Company networks often use a private certificate authority (CA). A TLS
inspection proxy or an internal server, such as an artifact store, uses
such a CA. Programs in the sandbox do not trust this CA by default, so
HTTPS requests to these servers fail.

Set `trust_host_cas = true` under `[msb.network]`:

``` toml
[msb.network]
trust_host_cas = true
```

msb-circus then creates the sandbox with `--trust-host-cas`. msb copies
every CA that the host trusts into the sandbox and adds it to the system
store of the sandbox.

The certificates must be in the CA store of the host. msb-circus does
not select or copy single certificates. The `[ca_certs]` table of
agent-circus is not supported; msb-circus logs a message and ignores it.
On Debian or Ubuntu hosts, put the certificate files into
`/usr/local/share/ca-certificates/` and run `sudo update-ca-certificates`
on the host.

Security note: every CA that the host trusts is also trusted in every
sandbox with this option. A CA that someone plants on the host is
trusted in the sandbox as well. The option is off by default.

The option applies only when the sandbox is created. After you change
it, or after you add or remove a CA on the host, re-create the sandbox:

``` shell
msb-circus rm -f && msb-circus up
```

msb-circus warns when the sandbox and the configuration disagree about
the option.

curl, git, and the Docker engine in the sandbox use the system store.
Containers that an agent starts with the Docker engine inside the
sandbox do not get these certificates.

## Troubleshooting

If the sandbox boots but the Docker engine stays unreachable, read the guest
log of dockerd:

``` shell
msb exec agents-<project> -- cat /tmp/dockerd.log
```

If a command fails with a message about the msb storage root and path
substitution, an ancestor of the msb storage root is writable by other
accounts. The msb storage root is `~/.microsandbox` by default. Fix the
directory named in the error. Then run `umask` in your shell to make sure
that new directories get safe permissions:

``` shell
chmod g-w,o-w ~/.microsandbox
```

If `up` or `start` reports that dockerd did not become ready, the warning
names the probe error and the diagnostic commands. Run the commands from the
warning and compare their output with the guest log above.

If `docker` commands fail with "Cannot connect to the Docker daemon" and
the boot log says that the docker data root is on overlayfs, docker is
not enabled for this sandbox. Add `docker_disk = "10G"` or
`root_disk = "flat:32G"` under `[msb.sandbox]`, then remove and re-create
the sandbox. See the Docker and Disks section.

If a forwarded port answers with `Empty reply from server`, see the
Port Forwarding section under Networking: the server inside the sandbox is
bound to `127.0.0.1` in the guest and must be restarted on `0.0.0.0`.

If `ssh-add -l` inside the sandbox reports that it cannot connect to the
agent, see the SSH Agent Forwarding section. Common causes are a missing
`[ssh]` table at sandbox creation, a stopped host agent, or a sandbox
created before the image rebuild.

If a name from your host hosts file does not resolve inside the sandbox,
see the Host Entries section. Check that the `[hosts]` table is present,
that the pattern matches the name, and that `msb-circus up` ran after the
last change to the host file.

If a mounted directory is empty inside the sandbox, make sure that the host
path in the configuration contains files. msb-circus fails the boot when a
host path does not exist or is not a directory, but it cannot see inside
the directory, so an empty source produces an empty guest directory.

## Limitations

msb is beta software. msb-circus pins the msb version through the
`microsandbox` package and warns about version drift, but command line flags
can still change between releases.

One workspace maps to one sandbox, and all harnesses of a workspace share
it. Commands in the sandbox run as root, because the Docker engine in the
guest needs root. There is no per-agent user separation yet.

msb-circus does not start sidecar services. Features of agent-circus that
need companion containers, for example llama.cpp or claude-mem, have no
msb-circus equivalent yet.
