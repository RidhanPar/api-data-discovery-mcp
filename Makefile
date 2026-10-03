.PHONY: install catalog generate test lint check

install:
	uv sync

catalog:
	uv run python -m nordlys_discovery.catalog

generate:
	uv run python -m scripts.catalog_gen

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

check: lint
	uv run python -m scripts.catalog_gen --check
	uv run pytest
