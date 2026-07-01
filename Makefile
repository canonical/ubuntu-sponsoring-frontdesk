.PHONY: all lint fmt test smoke clean

# Default: the checks CI runs -- lint then the unit suite.
all: lint test

lint:
	ruff check .

fmt:
	ruff format .

test:
	python3 -m pytest tests/ -q

# Read-only attribute check against real Launchpad. Usage: make smoke URL=<lp-url>
smoke:
	python3 smoke_test.py "$(URL)"

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache
