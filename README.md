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
(defun rpo/agent-shell-get-devcontainer-workspace-path (cwd)
  "Return devcontainer workspaceFolder for CWD, or default value if none found.

If .devcontainer/devcontainer.json is missing, fall back to
\"/workspaces/<project-name>\".

See https://containers.dev for more information on devcontainers."
  (let* ((devcontainer-config-file-name
          (expand-file-name ".devcontainer/devcontainer.json" cwd))
         (default-workspace-folder
          (concat "/workspaces/"
                  (file-name-nondirectory (directory-file-name cwd)))))
    (condition-case _err
        (map-elt (json-read-file devcontainer-config-file-name)
                 'workspaceFolder
                 default-workspace-folder)
      ;; Missing file => just use default.
      (file-missing default-workspace-folder)
      ;; Other problems => still hard errors.
      (permission-denied
       (error "Not readable: %s" devcontainer-config-file-name))
      (json-string-format
       (error "No valid JSON: %s" devcontainer-config-file-name)))))

(defun rpo/agent-shell-resolve-devcontainer-path (path)
  "Resolve PATH between local filesystem and devcontainer workspace.

Examples:

- /workspace/README.md
    => /home/xenodium/projects/kitchen-sink/README.md
- /home/xenodium/projects/kitchen-sink/README.md
    => /workspace/README.md"
  (let* ((cwd (agent-shell-cwd))
         (devcontainer-path (rpo/agent-shell-get-devcontainer-workspace-path cwd)))
    (if (string-prefix-p cwd path)
        ;; Local -> devcontainer
        (string-replace cwd devcontainer-path path)
      ;; Devcontainer -> local (with safety checks)
      (if agent-shell-text-file-capabilities
          (if-let* ((is-dev-container (string-prefix-p devcontainer-path path))
                    (local-path (expand-file-name
                                 (string-replace devcontainer-path cwd path))))
              (or
               (and (file-in-directory-p local-path cwd) local-path)
               (error "Resolves to path outside of working directory: %s" path))
            (error "Unexpected path outside of workspace folder: %s" path))
        (error "Refuse to resolve to local filesystem with text file capabilities disabled: %s" path)))))

(use-package agent-shell
  :ensure t
  :config
  (setq agent-shell-mistral-authentication
        (agent-shell-mistral-make-authentication :api-key "ignored"))
  (setq acp-logging-enabled t)
  (setq agent-shell-container-command-runner
        (lambda (buffer)
          "Return devcontainer command prefix."
          (let ((config (agent-shell-get-config buffer)))
            (pcase (map-elt config :identifier)
              ('claue-code '("devcontainer" "exec" "--workspace-folder" "." "--id-label" "io.devcontainer.exec-target=claude-code"))
              ('codex '("devcontainer" "exec" "--workspace-folder" "." "--id-label" "io.devcontainer.exec-target=codex"))
              ('mistral-vibe '("devcontainer" "exec" "--workspace-folder" "." "--id-label" "io.devcontainer.exec-target=mistral-vibe"))
              (_ '("devcontainer" "exec" "--workspace-folder" "."))))))
  (setq agent-shell-path-resolver-function #'rpo/agent-shell-resolve-devcontainer-path)
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
