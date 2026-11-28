VENV_DIR ?= .venv
PYTHON_SYS ?= python3.12
PYTHON ?= $(VENV_DIR)/bin/python
PIP ?= $(VENV_DIR)/bin/pip
PYTEST ?= $(VENV_DIR)/bin/pytest
RUFF ?= $(VENV_DIR)/bin/ruff
MYPY ?= $(VENV_DIR)/bin/mypy

.PHONY: setup test test-ci lint format run

setup:
	@test -d $(VENV_DIR) || $(PYTHON_SYS) -m venv $(VENV_DIR)
	$(PYTHON) -m pip install -r requirements.lock
	$(PYTHON) -m pip install --no-deps -e .

test:
	$(PYTEST) -m "not requires_ollama" --cov=src/carbon --cov-report=term-missing

test-ci:
	$(PYTEST) -m "not requires_ollama and not requires_macos" --cov=src/carbon --cov-report=term-missing

lint:
	$(RUFF) check .
	$(RUFF) format --check src tests scripts benchmarks
	$(MYPY) src

format:
	$(RUFF) format src tests scripts benchmarks

run:
	$(PYTHON) -m carbon.main
