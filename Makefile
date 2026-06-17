PYTHON ?= python3
RUFF   ?= ruff

.PHONY: help check lint format format-check test hooks bump install clean

help:
	@echo "ipmitui - common tasks"
	@echo ""
	@echo "  check         lint + format-check + test (what CI runs)"
	@echo "  lint          ruff check ."
	@echo "  format        ruff format ."
	@echo "  format-check  ruff format --check ."
	@echo "  test          python -m unittest discover -s tests -v"
	@echo "  hooks         pre-commit install (run the lint gate on every commit)"
	@echo "  bump          set the version (usage: make bump VERSION=1.1.0)"
	@echo "  install       pipx install . (uninstalls any existing ipmitui first)"
	@echo "  clean         drop caches + build artifacts"

check: lint format-check test

lint:
	$(RUFF) check .

format:
	$(RUFF) format .

format-check:
	$(RUFF) format --check .

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

hooks:
	pre-commit install

# Bump the single source of truth for the version. The release flow is:
# `make bump VERSION=X.Y.Z`, commit via a PR, merge; on a green main the
# tag-release job tags v<version> and the release job publishes it.
bump:
	@test -n "$(VERSION)" || { echo "usage: make bump VERSION=X.Y.Z"; exit 2; }
	sed -i 's/^__version__ = ".*"/__version__ = "$(VERSION)"/' src/ipmitui/__init__.py
	@grep -m1 '^__version__' src/ipmitui/__init__.py

install:
	pipx uninstall ipmitui 2>/dev/null || true
	pipx install .

clean:
	rm -rf dist build .ruff_cache .pytest_cache .mypy_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
