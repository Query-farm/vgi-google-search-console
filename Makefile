# Common tasks. `make help` lists them. Requires uv (https://docs.astral.sh/uv/).
# The SQL test targets follow the vgi-starters python template.

HAYBARN_VERSION ?= 2.0.1rc3
WORKER          := $(CURDIR)/.venv/bin/vgi-google-search-console
TOOLS           := $(CURDIR)/.tools
HAYBARN_UNITTEST := $(TOOLS)/bin/haybarn-unittest
TRANSPORT       ?= stdio

.DEFAULT_GOAL := help
.PHONY: help build tools lint test-unit test-sql test-sql-all test check clean

help: ## List the targets
	@awk 'BEGIN{FS=":.*## "} /^[a-zA-Z_-]+:.*## /{printf "  %-14s %s\n",$$1,$$2}' $(MAKEFILE_LIST)

build: ## Install the worker and dev tools into .venv
	uv sync --no-sources --locked

tools: ## Install the pinned SQL test runner into .tools
	@test -x $(HAYBARN_UNITTEST) || { uv venv -q $(TOOLS) && uv pip install -q -p $(TOOLS) haybarn-unittest==$(HAYBARN_VERSION); }

lint: build ## Static checks: ruff, mypy
	uv run --no-sources ruff check .
	uv run --no-sources ruff format --check .
	uv run --no-sources mypy

test-unit: build ## Offline pytest suite (live tests are deselected)
	uv run --no-sources pytest -q

test-sql: build tools ## SQL tests through DuckDB against the offline fake: make test-sql TRANSPORT=stdio|launch|http
	WORKER=$(WORKER) HAYBARN_UNITTEST=$(HAYBARN_UNITTEST) TRANSPORT=$(TRANSPORT) PYTHON="$(CURDIR)/.venv/bin/python" ci/run-integration.sh

test-sql-all: ## SQL tests over every transport
	$(MAKE) test-sql TRANSPORT=stdio
	$(MAKE) test-sql TRANSPORT=launch
	$(MAKE) test-sql TRANSPORT=http

test: test-unit test-sql ## Unit + SQL tests

check: lint test-unit test-sql-all ## Everything the offline CI runs

clean: ## Remove build outputs and tool caches
	rm -rf .venv .tools dist build .mypy_cache .ruff_cache .pytest_cache
