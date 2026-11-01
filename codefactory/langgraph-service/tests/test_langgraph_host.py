import sys
from types import ModuleType

import pytest
from langgraph_service import host
from langgraph_service.operations import WorkflowOperations


def _configure(monkeypatch, module_name):
    monkeypatch.setenv("LANGGRAPH_DATABASE_URL", "postgresql://langgraph-service")
    monkeypatch.setenv("GITHUB_SERVICE_URL", "http://github-service:8000")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "service-token")
    monkeypatch.setenv("WORKFLOW_OPERATIONS_MODULE", module_name)


def test_langgraph_host_requires_separate_database_and_service_contract(monkeypatch):
    monkeypatch.delenv("LANGGRAPH_DATABASE_URL", raising=False)
    monkeypatch.delenv("GITHUB_SERVICE_URL", raising=False)
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("WORKFLOW_OPERATIONS_MODULE", raising=False)

    with pytest.raises(ValueError, match="LANGGRAPH_DATABASE_URL"):
        host.create_app()


def test_langgraph_host_requires_operator_operations_factory(monkeypatch):
    module = ModuleType("worker_without_factory")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    _configure(monkeypatch, module.__name__)

    with pytest.raises(TypeError, match="must export make_operations"):
        host.create_app()


def test_langgraph_host_composes_standalone_worker_app(monkeypatch):
    module = ModuleType("configured_worker")
    module.make_operations = lambda: WorkflowOperations()
    monkeypatch.setitem(sys.modules, module.__name__, module)
    _configure(monkeypatch, module.__name__)

    app = host.create_app()

    assert any(route.path == "/healthz" for route in app.routes)
