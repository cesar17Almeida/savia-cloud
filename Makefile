# savia-cloud -- local development tasks.

VENV := .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

.DEFAULT_GOAL := help
.PHONY: help run install

help: ## List the available targets
	@awk -F':.*## ' '/^[a-zA-Z_-]+:.*## /{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

run: | $(PY) .env ## Run the backend locally (http://localhost:8000/health)
	@set -a; . ./.env; set +a; \
	$(if $(HOST),HOST=$(HOST)) $(if $(PORT),PORT=$(PORT)) $(PY) run.py

install: | $(PY) ## Create .venv and install requirements.txt
	$(PIP) install -q -r requirements.txt

# Bootstrap: the venv and the local .env are created on first `make run`.
$(PY):
	python3 -m venv $(VENV)
	$(PIP) install -q -r requirements.txt

.env:
	@cp .env.example .env
	@echo "created .env from .env.example -- fill TTN_API_KEY / WEBHOOK_SECRET / CRON_SECRET"
