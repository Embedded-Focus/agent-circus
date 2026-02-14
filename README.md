# AI Agents Circus

This repository contains my AI agents setup. Each agent is running in its respective service.

IDEs interface with agents via the [Agent Client Protocol](https://agentclientprotocol.com/) (ACP).

## Configuring the Environment

### Installing the `agent-circus` Tool

``` shell
uv tool install .
```

You can then remove containers of a DevContainer environment by issuing `agent-circus`.

See the [uv tool documentation](https://docs.astral.sh/uv/concepts/tools/) on how to work with tools in general.

### Setting up Editors to Work with ACP: Emacs

This is my current `agent-shell` configuration:

``` emacs-lisp
(defconst rpo/agent-shell--container-workspace-path "/workspace/"
  "The workspace path inside agent containers.")

(defun rpo/agent-shell--resolve-container-path (path)
  "Resolve PATH between local filesystem and container workspace.

For example:

- /workspace/README.md
    => /home/xenodium/projects/kitchen-sink/README.md
- /home/xenodium/projects/kitchen-sink/README.md
    => /workspace/README.md"
  (let ((cwd (agent-shell-cwd)))
    (if (string-prefix-p cwd path)
        ;; Local -> container
        (string-replace cwd rpo/agent-shell--container-workspace-path path)
      ;; Container -> local
      (if agent-shell-text-file-capabilities
          (if-let* ((_ (string-prefix-p rpo/agent-shell--container-workspace-path path))
                    (local-path (expand-file-name
                                 (string-replace rpo/agent-shell--container-workspace-path cwd path))))
              (or
               (and (file-in-directory-p local-path cwd) local-path)
               (error "Resolves to path outside of working directory: %s" path))
            (error "Unexpected path outside of workspace folder: %s" path))
        (error "Refuse to resolve to local filesystem with text file capabilities disabled: %s" path)))))

(defun rpo/agent-shell-compose-runner-multi (buffer)
  "Return the docker compose command prefix to run for BUFFER's agent.

Looks up the agent identifier in BUFFER's `agent-shell' config and
selects the matching `docker compose exec` service, defaulting to
\"claude-code\" when no identifier-specific override is found."
  (let* ((cfg (agent-shell-get-config buffer))
         (id  (map-elt cfg :identifier))
         (service
          (pcase id
            ('claude-code "claude-code")
            ('codex "codex")
            ('mistral-vibe "mistral-vibe")
            (_ "claude-code")))
         (prefix '("docker" "compose" "-f" ".agent-circus/compose.yaml" "exec" "-ti")))
    (append prefix (list service))))

(use-package agent-shell
  :ensure t
  :config
  (setq agent-shell-mistral-authentication
        (agent-shell-mistral-make-authentication :api-key "ignored"))
  (setq acp-logging-enabled t)
  (setq agent-shell-container-command-runner #'rpo/agent-shell-compose-runner-multi)
  (setq agent-shell-path-resolver-function #'rpo/agent-shell--resolve-container-path)
  (setq agent-shell-file-completion-enabled t))
```

## Working with the Environment

``` shell
# Install agent configuration/environment files
agent-circus init --deploy

# Generate container images
agent-circus build

# Create containers
agent-circus up

# Remove all related resources
agent-circus remove                     # just remove containers
agent-circus remove --destroy           # also remove agent configuration/environment files
agent-circus remove --destroy  --force  # don't ask for permission
```

## Uninstalling

``` shell
uv tool uninstall agent-circus
```
