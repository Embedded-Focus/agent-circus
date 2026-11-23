;;; agent-shell-circus.el --- agent-shell integration for agent-circus -*- lexical-binding: t; -*-

;; Version: 0.2.0
;; Package-Requires: ((emacs "28.1"))
;; Keywords: agents, acp, agent-shell

;;; Commentary:

;; This file wires `agent-shell' to agent-circus. agent-circus runs agent
;; harnesses in a Microsandbox micro-VM and speaks ACP on its `exec'
;; subcommand:
;;
;;   agent-circus exec --acp -- <harness>
;;
;; `agent-shell-command-prefix' is only a PREFIX: agent-shell appends the
;; agent's own ACP command (for example `agent-shell-opencode-acp-command',
;; \='("opencode" "acp")) after it. The runner therefore returns the exec
;; prefix only; the per-agent defaults of agent-shell name the same
;; harnesses that ship in the sandbox image (opencode acp, pi-acp,
;; claude-agent-acp, codex-acp).
;;
;; agent-circus mounts the project at /workspace inside the sandbox. If you
;; change the mount in `[msb.mounts]', change
;; `rpo/agent-shell-circus-workspace-path' to match it.
;;
;; API keys stay out of this file. agent-circus reads `env_passthrough'
;; from its configuration and forwards those variables from the host.
;;
;; Logging: the ACP backend (acp.el) treats every non-empty stderr chunk
;; from the agent process as an API error and renders an error dialog for
;; it. agent-circus therefore silences stderr in ACP mode (`exec --acp')
;; and logs to the --log-file only; without --log-file it falls back to
;; /tmp/agent-circus-acp.log. Keep --log-file here so the logs land in a
;; predictable place.
;;
;; For agent-circus 1.x (Docker or Podman), use the configuration in the
;; README of the `v1' branch instead.
;;
;; Usage: put this file on your `load-path', then in your init file:
;;
;;   (require 'agent-shell-circus)

;;; Code:

(require 'agent-shell)

(defgroup agent-shell-circus nil
  "agent-shell integration for agent-circus."
  :group 'agent-shell)

(defcustom rpo/agent-shell-circus-workspace-path "/workspace/"
  "Workspace path inside the agent-circus sandbox, with a trailing slash."
  :type 'string
  :group 'agent-shell-circus)

(defun rpo/agent-shell-circus-resolve-path (path)
  "Resolve PATH between local filesystem and the sandbox workspace.

For example:

- /workspace/README.md
    => /home/you/projects/my-project/README.md
- /home/you/projects/my-project/README.md
    => /workspace/README.md"
  (let ((cwd (agent-shell-cwd))
        (guest-path rpo/agent-shell-circus-workspace-path))
    (if (string-prefix-p cwd path)
        ;; Local -> sandbox
        (string-replace cwd guest-path path)
      ;; Sandbox -> local
      (if agent-shell-text-file-capabilities
          (if-let* ((_ (string-prefix-p guest-path path))
                    (local-path (expand-file-name
                                 (string-replace guest-path cwd path))))
              (or
               (and (file-in-directory-p local-path cwd) local-path)
               (error "Resolves to path outside of working directory: %s" path))
            (error "Unexpected path outside of workspace folder: %s" path))
        (error "Refuse to resolve to local filesystem with text file capabilities disabled: %s" path)))))

(defun rpo/agent-shell-circus-runner (_buffer)
  "Return the agent-circus exec command prefix for any agent.

The prefix is the same for every agent-shell buffer, so the buffer
argument is unused.

This returns the prefix only. agent-shell appends the agent's own
ACP command (for example \\='(\"opencode\" \"acp\") for OpenCode)
after the prefix. Do NOT add a harness command to the prefix: the
doubled command makes the guest harness reject its arguments."
  (list "agent-circus" "--log-level" "DEBUG"
        "--log-file" "/tmp/agent-circus.log"
        "exec" "--acp" "--"))

(with-eval-after-load 'agent-shell
  (setq acp-logging-enabled t)
  (setq agent-shell-command-prefix #'rpo/agent-shell-circus-runner)
  (setq agent-shell-path-resolver-function #'rpo/agent-shell-circus-resolve-path)
  (setq agent-shell-file-completion-enabled t))

(provide 'agent-shell-circus)
;;; agent-shell-circus.el ends here
