.PHONY: all fmt clean \
	usf-check usf-lint usf-test usf-smoke \
	charm-check charm-lint charm-unit charm-integration charm-lock

# Two halves with two interpreters, hence two prefixes (#146):
#
#   usf-*    the bot, on SYSTEM python3 -- apt_pkg (python3-apt) is a
#            compiled extension tied to it, so there is no virtualenv.
#   charm-*  the charm, in a uv-managed venv -- ops isn't packaged for the
#            system interpreter, and the charm part is built with uv anyway.
#
# Ruff is one config (pyproject.toml) for both, pinned to the same version
# in both places (#108): system ruff for usf-*, the dev extra for charm-*.

# Default: everything CI runs -- both halves' lint and unit suites.
all: usf-check charm-check

fmt:
	ruff check --fix .
	ruff format .

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .coverage .venv *.charm

# --- the bot (usf/) --------------------------------------------------------

usf-check: usf-lint usf-test

usf-lint:
	ruff check usf tools
	ruff format --check --diff usf tools

usf-test:
	python3 -m pytest usf/tests/ -q

# Read-only attribute check against real Launchpad. Usage: make usf-smoke URL=<lp-url>
usf-smoke:
	python3 usf/smoke_test.py "$(URL)"

# --- the charm (src/, tests/) ----------------------------------------------

UV_RUN := uv run --extra dev

charm-check: charm-lint charm-unit

charm-lint:
	$(UV_RUN) ruff check src tests
	$(UV_RUN) ruff format --check --diff src tests

charm-unit:
	PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 $(UV_RUN) python -m pytest tests/unit -q $(ARGS)

# Deploys to a temporary model on the current juju controller; packs the
# charm first unless CHARM_PATH points at one. Never part of `all`/CI.
charm-integration:
	$(UV_RUN) python -m pytest tests/integration -v --log-cli-level=INFO \
		$(if $(CHARM_PATH),--charm-path=$(CHARM_PATH)) $(ARGS)

charm-lock:
	uv lock -U
