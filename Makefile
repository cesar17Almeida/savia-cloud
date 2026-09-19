# savia-cloud -- local development tasks.

VENV := .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

.DEFAULT_GOAL := help
.PHONY: help run install test demo demo-reset

help: ## List the available targets
	@awk -F':.*## ' '/^[a-zA-Z_-]+:.*## /{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

run: | $(PY) .env ## Run the backend locally (http://localhost:8000/health)
	@set -a; . ./.env; set +a; \
	$(if $(HOST),HOST=$(HOST)) $(if $(PORT),PORT=$(PORT)) $(PY) run.py

install: | $(PY) ## Create .venv and install requirements.txt
	$(PIP) install -q -r requirements.txt

test: | $(PY) ## Run the test suite
	$(VENV)/bin/pytest -q

demo: | $(PY) .env.demo ## Run with the station link over HTTP (see DEMO.md)
	@set -a; . ./.env.demo; $(if $(HOST),HOST=$(HOST);) $(if $(PORT),PORT=$(PORT);) set +a; \
	ip=$$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname); \
	echo ""; \
	echo "  TerraLink (URL del servidor):  http://$$ip:$${PORT:-8000}"; \
	echo "  Panel:                         http://localhost:$${PORT:-8000}/home/   (admin / $$ADMIN_PASSWORD)"; \
	echo ""; \
	$(PY) run.py

demo-reset: ## Delete the local database of `make demo`
	rm -f demo.db

# Bootstrap: the venv and the local .env are created on first `make run`.
$(PY):
	python3 -m venv $(VENV)
	$(PIP) install -q -r requirements.txt

.env.demo:
	@cp .env.demo.example .env.demo
	@echo "created .env.demo from .env.demo.example"

.env:
	@cp .env.example .env
	@echo "created .env from .env.example -- fill TTN_API_KEY / WEBHOOK_SECRET / CRON_SECRET"
