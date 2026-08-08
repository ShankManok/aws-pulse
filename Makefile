SHELL := /bin/bash
.PHONY: install test lint deploy clean

install:
	cd infra && npm install
	pip install -r requirements.txt -r requirements-dev.txt

test:
	pytest tests/unit/ -v --cov=src --cov-report=term-missing

test-integration:
	pytest tests/integration/ -v --stage=dev

test-e2e:
	pytest tests/e2e/ -v --stage=dev

lint:
	ruff check src/ tests/
	ruff format --check src/ tests/
	cd infra && npx eslint lib/

format:
	ruff format src/ tests/

synth:
	cd infra && npx cdk synth

deploy-dev:
	cd infra && npx cdk deploy --all --context stage=dev --require-approval never

deploy-prod:
	cd infra && npx cdk deploy --all --context stage=prod

clean:
	rm -rf cdk.out/ node_modules/ .venv/ __pycache__/ .pytest_cache/ htmlcov/
	find . -type d -name __pycache__ -exec rm -rf {} +

package-lambda:
	@echo "Packaging Lambda layers..."
	pip install -r requirements.txt -t build/python/
	cd build && zip -r ../lambda-layer.zip python/
