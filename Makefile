.DEFAULT_GOAL := help
DOTENV        := .env

.PHONY: help test lint format check-format type-check check audit pre-commit update-templates update-templates-dryrun sops-edit sops-decrypt sops-encrypt sops-updatekeys

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-24s\033[0m %s\n", $$1, $$2}'

test: ## Run tests
	uv run pytest

lint: ## Run Ruff lint checks
	uv run ruff check

format: ## Format Python files with Ruff
	uv run ruff format

check-format: ## Check Python formatting with Ruff
	uv run ruff format --check

type-check: ## Run type checks
	uv run ty check

check: check-format lint type-check test ## Run all checks

audit: ## Audit locked Python dependencies
	uv audit --locked
	uv --directory src/agent_circus/templates/agent-circus audit --locked

pre-commit: ## Run pre-commit hooks on all tracked files
	uv run pre-commit run --all-files

update-templates-dryrun: ## Print current vs. latest pinned versions in the agent-circus template
	uv run agent-circus-update-templates

update-templates: ## Write latest pinned versions into the agent-circus template
	uv run agent-circus-update-templates --apply

sops-edit: ## Edit a SOPS-encrypted file
	sops edit $(DOTENV)

sops-decrypt: ## Decrypt a SOPS file in-place
	sops --decrypt --in-place $(DOTENV)

sops-encrypt: ## Encrypt a file in-place with SOPS
	sops --encrypt --in-place $(DOTENV)

sops-updatekeys: ## Update SOPS encryption keys
	sops updatekeys --yes $(DOTENV)
