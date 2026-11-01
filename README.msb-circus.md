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
- [pi-acp](https://github.com/svkozak/pi-acp), the ACP adapter for Pi

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

`ps --all` lists every sandbox that msb knows about, not only the sandbox
of this workspace. `ps --running` lists only running sandboxes.

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
```

`exec` boots the sandbox first when it is not running. The workspace is
mounted at the same absolute path as on the host, so file paths agree
between your editor and the agent.

### ACP Sessions

For an ACP session from an editor or IDE, add `--acp`:

``` shell
msb-circus exec --acp -- pi-acp
msb-circus exec --acp -- opencode acp
```

In ACP mode, msb-circus streams the session between your editor and the
sandbox without a pseudo-terminal. Standard output carries only ACP
messages. Diagnostics go to standard error. The stream copies bytes without
change, so large messages and non-ASCII text arrive intact. When the editor
disconnects, msb-circus stops the agent process in the sandbox.

## Setting up Editors to Work with ACP

### Emacs

This is a working [agent-shell](https://github.com/xenodium/agent-shell)
configuration based on msb-circus. The runner starts the harness through
`msb-circus exec --acp`, so the editor talks to the sandbox over ACP. The
path resolver maps file paths between your editor and the `/workspace`
mount in the sandbox.

The sandbox carries Pi and OpenCode. An identifier without a harness in the
sandbox falls back to Pi through the pi-acp adapter. API keys do not go into
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
selects the matching harness in the sandbox. The sandbox carries Pi
and OpenCode; every other identifier falls back to Pi through the
pi-acp adapter."
  (let* ((cfg (agent-shell-get-config buffer))
         (id  (map-elt cfg :identifier))
         (harness
          (pcase id
            ('opencode '("opencode" "acp"))
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
`env_passthrough`, and `additional_dirs` keys of agent-circus. Every other
key produces a warning. agent-circus in turn logs a warning for the unknown
`[msb]` table and continues.

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
# cpus = 2                             # default: 1

[msb.mounts]
"." = "/workspace"                     # default; more mounts: "/data" = "/data:ro"

[msb.network]
mode = "public,private"                # default; unrestricted network access

[msb.agent]
command = ["pi"]                       # default exec target
workdir = "/workspace"
```

All fields are optional. The values above are the defaults.

## Secrets

API keys never go into the image. msb-circus passes them per command with
`--env KEY=VALUE`, or you list them once under `env_passthrough`:

``` shell
msb-circus exec -e OPENAI_API_KEY=sk-... -- pi
```

msb-circus reads the variables under `env_passthrough` from your host shell
at every command. If a variable is unset, the command fails before msb
starts. Log output never contains secret values.

## Networking

By default, a sandbox starts with `--net "public,private"`. This gives the
sandbox unrestricted network access, which the Docker engine in the guest
needs to pull images. Your model API endpoints and MCP servers are reachable
through this profile.

msb-circus does not yet translate a network policy into msb network rules.
Until that feature exists, treat the default as an open profile and keep
sensitive host services outside the reach of the sandbox.

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
