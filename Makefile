.PHONY: help install lint test test-runtime test-lambdas test-cli test-infra synth image

help:
	@echo "install       Install dependencies for all sub-projects"
	@echo "lint          Run ruff on Python projects and tsc on infra"
	@echo "test          Run all unit tests"
	@echo "synth         Synthesize the CDK stack"
	@echo "image         Build the runtime container image locally (linux/arm64)"

install:
	cd runtime && uv sync
	cd lambdas && uv sync
	cd cli && uv sync
	cd infra && npm ci

lint:
	cd runtime && uv run ruff check src tests
	cd lambdas && uv run ruff check .
	cd cli && uv run ruff check src tests
	cd infra && npx tsc --noEmit

test: test-runtime test-lambdas test-cli test-infra

test-runtime:
	cd runtime && uv run pytest -q

test-lambdas:
	cd lambdas && uv run pytest -q

test-cli:
	cd cli && uv run pytest -q

test-infra:
	cd infra && npx jest

synth:
	cd infra && npx cdk synth

image:
	docker build --platform linux/arm64 -t kiro-agentcore-runtime runtime
