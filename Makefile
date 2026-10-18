.PHONY: dev web web-watch test lint fmt check docs docs-watch deb

dev:            ## run the server with the current build of the UI
	uv run texas2step serve --host 127.0.0.1 --port 8080

web:            ## typecheck and bundle the TypeScript UI into src/texas2step/static
	cd web && npm ci && npm run check

web-watch:      ## rebuild the UI on change (unminified, with source maps)
	cd web && npm run watch

test:
	uv run pytest

lint:
	uv run ruff check src tests scripts
	uv run ruff format --check src tests scripts
	cd web && npx tsc --noEmit

fmt:
	uv run ruff format src tests scripts
	uv run ruff check --fix src tests scripts

check: lint test

docs:           ## render every .md file into the static site in html/
	uv run python scripts/build_docs.py

docs-watch:     ## re-render html/ whenever a .md file changes
	uv run python scripts/build_docs.py --watch

deb:            ## build dist/texas2step_<version>_amd64.deb (Ubuntu 26.04)
	packaging/build-deb.sh
