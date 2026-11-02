PYTHON ?= python3
KUBECTL ?= kubectl

CODEFACTORY_PYTHONPATH := codefactory/github-service/src:codefactory/langgraph-service/src:codefactory/management-portal/src
CODEFACTORY_TESTS := codefactory/github-service/tests codefactory/langgraph-service/tests codefactory/management-portal/tests codefactory/integration-tests
INFERENCE_TESTS := inference-platform/inference/llama-cpp-smoke/test_evaluate_32b.py

.PHONY: install-dev test lint format kustomize check

install-dev:
	$(PYTHON) -m pip install -e 'codefactory/github-service[test]' -e 'codefactory/langgraph-service[test]' -e 'codefactory/management-portal[test]'

test:
	PYTHONPATH='$(CODEFACTORY_PYTHONPATH)' $(PYTHON) -m pytest -c codefactory/github-service/pyproject.toml $(CODEFACTORY_TESTS) $(INFERENCE_TESTS)

lint:
	$(PYTHON) -m ruff check codefactory/github-service codefactory/langgraph-service codefactory/management-portal codefactory/integration-tests
	$(PYTHON) -m ruff format --check codefactory/github-service codefactory/langgraph-service codefactory/management-portal codefactory/integration-tests

format:
	$(PYTHON) -m ruff format codefactory/github-service codefactory/langgraph-service codefactory/management-portal codefactory/integration-tests

kustomize:
	@set -e; for overlay in infra-codefactory/kubernetes/environments/dev infra-codefactory/kubernetes/environments/staging infra-codefactory/kubernetes/environments/prod inference-platform/kubernetes; do $(KUBECTL) kustomize "$$overlay" >/dev/null; done

check: test lint kustomize