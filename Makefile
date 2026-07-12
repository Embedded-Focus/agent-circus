.DEFAULT_GOAL := help

.PHONY: help update-templates update-templates-apply

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-24s\033[0m %s\n", $$1, $$2}'

update-templates: ## Print current vs. latest pinned versions in the agent-circus template
	uv run agent-circus-update-templates

update-templates-apply: ## Write latest pinned versions into the agent-circus template
	uv run agent-circus-update-templates --apply
