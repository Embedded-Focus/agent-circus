;;; agent-shell-circus.el --- agent-shell backends for agent-circus and msb-circus -*- lexical-binding: t; -*-

;; Version: 0.1.0
;; Package-Requires: ((emacs "28.1"))
;; Keywords: agents, acp, agent-shell

;;; Commentary:

;; This file wires `agent-shell' to either agent-circus or msb-circus.
;; Both tools run agent harnesses in an isolated guest and speak ACP on
;; their `exec' subcommand:
;;
;;   agent-circus exec <service> --   (Docker or Podman container)
;;   msb-circus exec --acp -- <harness>   (Microsandbox micro-VM)
;;
;; Pick the backend with `rpo/agent-shell-circus-backend', or call
;; `rpo/agent-shell-circus-set-backend' at runtime. New sessions use the
;; selected backend; running sessions keep the one they started with.
;;
;; Both backends mount the project at /workspace inside the guest, so one
;; path resolver serves both. If you change the mount in your
;; configuration, change the two constants below to match it.
;;
;; API keys stay out of this file. agent-circus reads host credentials and
;; env_passthrough from .agent-circus/config.toml; msb-circus reads
;; env_passthrough from the same file.
;;
;; Logging: msb-circus writes its own logs to stderr, but the ACP backend
;; (acp.el) treats every non-empty stderr chunk from the agent process as
;; an API error and renders an error dialog for it. msb-circus therefore
;; silences stderr in ACP mode (`exec --acp`) and logs to the --log-file
;; only; without --log-file it falls back to /tmp/msb-circus-acp.log. Keep
;; --log-file here so the logs land in a predictable place.
;;
;; Usage: put this file on your `load-path', then in your init file:
;;
;;   (require 'agent-shell-circus)

;;; Code:

(require 'map)
(require 'agent-shell)

(defgroup agent-shell-circus nil
  "agent-shell integration for agent-circus and msb-circus."
  :group 'agent-shell)

(defconst rpo/agent-shell-circus-container-workspace-path "/workspace/"
  "Workspace path inside an agent-circus container.")

(defconst rpo/agent-shell-circus-msb-workspace-path "/workspace/"
  "Workspace path inside a msb-circus sandbox.")

(defcustom rpo/agent-shell-circus-backend 'msb-circus
  "Which circus backend agent-shell uses for new sessions.
`agent-circus' runs harnesses in Docker or Podman containers.
`msb-circus' runs harnesses in a Microsandbox micro-VM."
  :type '(radio
          (const :tag "agent-circus (Docker or Podman)" agent-circus)
          (const :tag "msb-circus (Microsandbox)" msb-circus))
  :group 'agent-shell-circus)

(defun rpo/agent-shell-circus-set-backend (backend)
  "Select BACKEND (`agent-circus' or `msb-circus') for new sessions.
Running sessions keep the backend they started with."
  (interactive
   (list (intern (completing-read "Backend: "
                                  '("agent-circus" "msb-circus")
                                  nil t))))
  (unless (memq backend '(agent-circus msb-circus))
    (user-error "Unknown backend: %s" backend))
  (setq rpo/agent-shell-circus-backend backend)
  (message "agent-shell will use %s for new sessions." backend))

(defun rpo/agent-shell-circus--workspace-path ()
  "Return the workspace path inside the guest of the selected backend."
  (pcase rpo/agent-shell-circus-backend
    ('msb-circus rpo/agent-shell-circus-msb-workspace-path)
    (_ rpo/agent-shell-circus-container-workspace-path)))

(defun rpo/agent-shell-circus--agent-circus-service (identifier)
  "Return the agent-circus service name for IDENTIFIER."
  (pcase identifier
    ('claude-code "claude-code")
    ('codex "codex")
    ('mistral-vibe "mistral-vibe")
    ('opencode "opencode")
    ('pi "pi")
    (_ "opencode")))

(defun rpo/agent-shell-circus--msb-harness (identifier)
  "Return the msb-circus harness command for IDENTIFIER.

The msb-circus sandbox carries Pi and OpenCode. Every identifier
without a harness in the sandbox falls back to Pi through the
pi-acp adapter."
  (pcase identifier
    ('opencode '("opencode" "acp"))
    (_ '("pi-acp"))))

(defun rpo/agent-shell-circus-resolve-path (path)
  "Resolve PATH between local filesystem and the guest workspace.

For example:

- /workspace/README.md
    => /home/you/projects/my-project/README.md
- /home/you/projects/my-project/README.md
    => /workspace/README.md"
  (let ((cwd (agent-shell-cwd))
        (guest-path (rpo/agent-shell-circus--workspace-path)))
    (if (string-prefix-p cwd path)
        ;; Local -> guest
        (string-replace cwd guest-path path)
      ;; Guest -> local
      (if agent-shell-text-file-capabilities
          (if-let* ((_ (string-prefix-p guest-path path))
                    (local-path (expand-file-name
                                 (string-replace guest-path cwd path))))
              (or
               (and (file-in-directory-p local-path cwd) local-path)
               (error "Resolves to path outside of working directory: %s" path))
            (error "Unexpected path outside of workspace folder: %s" path))
        (error "Refuse to resolve to local filesystem with text file capabilities disabled: %s" path)))))

(defun rpo/agent-shell-circus-runner (buffer)
  "Return the exec command prefix to run for BUFFER's agent.

The command prefix depends on `rpo/agent-shell-circus-backend'.
agent-circus selects the service by identifier. msb-circus always
runs in ACP mode and selects the harness by identifier."
  (let ((id (map-elt (agent-shell-get-config buffer) :identifier)))
    (pcase rpo/agent-shell-circus-backend
      ('msb-circus
       (append (list "msb-circus" "--log-level" "DEBUG"
                     "--log-file" "/tmp/msb-circus.log"
                     "exec" "--acp" "--")
               (rpo/agent-shell-circus--msb-harness id)))
      (_
       (append (list "agent-circus" "--log-level" "DEBUG"
                     "--log-file" "/tmp/agent-circus.log"
                     "exec")
               (list (rpo/agent-shell-circus--agent-circus-service id) "--"))))))

(with-eval-after-load 'agent-shell
  (setq acp-logging-enabled t)
  (setq agent-shell-command-prefix #'rpo/agent-shell-circus-runner)
  (setq agent-shell-path-resolver-function #'rpo/agent-shell-circus-resolve-path)
  (setq agent-shell-file-completion-enabled t))

(provide 'agent-shell-circus)
;;; agent-shell-circus.el ends here
