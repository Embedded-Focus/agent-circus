.DEFAULT_GOAL := help
DOTENV        := .env

.PHONY: help update-templates update-templates-apply sops-edit sops-decrypt sops-encrypt sops-updatekeys

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-24s\033[0m %s\n", $$1, $$2}'

update-templates: ## Print current vs. latest pinned versions in the agent-circus template
	uv run agent-circus-update-templates

update-templates-apply: ## Write latest pinned versions into the agent-circus template
	uv run agent-circus-update-templates --apply

sops-edit: ## Edit a SOPS-encrypted file
	sops edit $(DOTENV)

sops-decrypt: ## Decrypt a SOPS file in-place
	sops --decrypt --in-place $(DOTENV)

sops-encrypt: ## Encrypt a file in-place with SOPS
	sops --encrypt --in-place $(DOTENV)

sops-updatekeys: ## Update SOPS encryption keys
	sops updatekeys --yes $(DOTENV)
