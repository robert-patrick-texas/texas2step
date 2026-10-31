.PHONY: dev web web-watch test lint fmt check docs docs-watch sheet deb version upgrade t2s t2s-test

# t2s, the Go client: the platforms it is built for (Go's names; darwin is macOS), each a
# file of its own in dist/.
T2S_TARGETS = linux/amd64 linux/arm64 darwin/amd64 darwin/arm64 windows/amd64 windows/arm64

dev:            ## run the server with the current build of the UI
	TEXAS2STEP_CLIENTS_FILE=texas2step-clients.db uv run texas2step serve --host 127.0.0.1 --port 8080

web:            ## typecheck and bundle the TypeScript UI into src/texas2step/static
	cd web && npm ci && npm run check

web-watch:      ## rebuild the UI on change (unminified, with source maps)
	cd web && npm run watch

test:
	uv run pytest

lint:
	uv run ruff check src tests scripts packaging
	uv run ruff format --check src tests scripts packaging
	cd web && npx tsc --noEmit

fmt:
	uv run ruff format src tests scripts packaging
	uv run ruff check --fix src tests scripts packaging

check: lint test t2s-test

docs:           ## render every .md file into the static site in html/
	uv run python scripts/build_docs.py

docs-watch:     ## re-render html/ whenever a .md file changes
	uv run python scripts/build_docs.py --watch

sheet:          ## print the one-page sheet on US Letter and A4 (docs/at-a-glance*.pdf) with Playwright's Chromium
	uv run --with playwright python scripts/build_sheet.py

deb:            ## build dist/texas2step-<version>-1-amd64.deb and dist/texas2step-setup (Ubuntu 26.04)
	packaging/build-deb.sh

upgrade:        ## move every dependency to its newest release, then rebuild the UI
	uv lock --upgrade
	uv sync
	cd web && npm install --save-dev --save-exact $$(node -p \
	    "Object.keys(require('./package.json').devDependencies).map((d) => d + '@latest').join(' ')")
	$(MAKE) web
	cd t2s && go get -u ./... && go mod tidy

version:        ## print the version; it is set only in src/texas2step/__init__.py
	@sed -n 's/^__version__ = "\(.*\)"$$/\1/p' src/texas2step/__init__.py

t2s:            ## build t2s for Linux, macOS and Windows into dist/: t2s-linux-amd64, t2s-macos-arm64, ...
	@v=$$($(MAKE) -s version); cd t2s && for target in $(T2S_TARGETS); do \
	    os=$${target%/*}; arch=$${target#*/}; name=$$os; ext=; \
	    [ $$os = darwin ] && name=macos; [ $$os = windows ] && ext=.exe; \
	    echo "t2s $$v for $$name/$$arch"; \
	    CGO_ENABLED=0 GOOS=$$os GOARCH=$$arch go build -trimpath \
	        -ldflags "-s -w -X main.version=$$v" -o ../dist/t2s-$$name-$$arch$$ext . || exit 1; \
	done

t2s-test:       ## gofmt, go vet (Linux, macOS and Windows) and go test for t2s
	@cd t2s && test -z "$$(gofmt -l .)" || { echo "gofmt would change:"; gofmt -l .; exit 1; }
	cd t2s && go vet ./... && GOOS=darwin go vet ./... && GOOS=windows go vet ./...
	cd t2s && go test ./...
