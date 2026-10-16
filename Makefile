.PHONY: setup up seed check demo load-test test test-live lint spec check-secrets down clean

setup:            ## install deps into .venv
	uv sync

up:               ## start WooCommerce + nginx (TLS, rate limit) on https://localhost:8443
	docker compose up -d

seed:             ## install WooCommerce, fake data, read-only key -> docker/out/keys.env
	docker compose run --rm wpcli

check:            ## prove the auth flow against the store
	uv run woo-bridge check

demo:             ## real MCP client <-> server over stdio, plays the support scenarios
	uv run python scripts/demo.py

load-test:        ## rate-limit behaviour against nginx's real 429s
	uv run python scripts/load_test.py

test:             ## offline tests (no network)
	uv run pytest

test-live:        ## contract tests against the Docker store
	uv run pytest -m live

lint:
	uv run ruff check . && uv run ruff format --check .

check-secrets:    ## fail if a real key, private key or token is in a tracked file
	! git grep -nIE 'ck_[0-9a-f]{40}|cs_[0-9a-f]{40}|BEGIN [A-Z ]*PRIVATE KEY|rzp_(live|test)_[A-Za-z0-9]{10,}'

spec:             ## regenerate docs/tool_spec.json from code
	uv run woo-bridge spec

down:
	docker compose down

clean:            ## remove containers, volumes and generated keys/certs
	docker compose down -v
	rm -f docker/out/* docker/certs/*
