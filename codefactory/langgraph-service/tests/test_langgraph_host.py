import asyncio
import json
import sys
from types import ModuleType

import pytest
from langgraph_service import migrate
from langgraph_service.database import postgres_url_from_environment
from langgraph_service import host
from langgraph_service.operations import WorkflowOperations
from langgraph_service.workflow_operations import make_operations
from sqlalchemy.engine import make_url


def _configure(monkeypatch, module_name):
    for key, value in {
        "LANGGRAPH_DB_HOST": "postgres.example.test",
        "LANGGRAPH_DB_PORT": "5432",
        "LANGGRAPH_DB_NAME": "langgraph",
        "LANGGRAPH_DB_USERNAME": "langgraph-user",
        "LANGGRAPH_DB_PASSWORD": "langgraph-secret",
        "LANGGRAPH_DB_SSLMODE": "verify-full",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GITHUB_SERVICE_URL", "http://github-service:8000")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "service-token")
    monkeypatch.setenv("WORKFLOW_OPERATIONS_MODULE", module_name)


def test_langgraph_host_requires_separate_database_and_service_contract(monkeypatch):
    for suffix in ("HOST", "PORT", "NAME", "USERNAME", "PASSWORD", "SSLMODE"):
        monkeypatch.delenv(f"LANGGRAPH_DB_{suffix}", raising=False)
    monkeypatch.delenv("GITHUB_SERVICE_URL", raising=False)
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("WORKFLOW_OPERATIONS_MODULE", raising=False)

    with pytest.raises(ValueError, match="LANGGRAPH_DB_HOST"):
        host.create_app()


def test_langgraph_host_does_not_fallback_to_database_url(monkeypatch):
    monkeypatch.setenv("LANGGRAPH_DATABASE_URL", "postgresql://legacy/db")
    for suffix in ("HOST", "PORT", "NAME", "USERNAME", "PASSWORD", "SSLMODE"):
        monkeypatch.delenv(f"LANGGRAPH_DB_{suffix}", raising=False)
    monkeypatch.setenv("GITHUB_SERVICE_URL", "http://github-service:8000")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "service-token")
    monkeypatch.setenv("WORKFLOW_OPERATIONS_MODULE", "unused_worker_module")

    with pytest.raises(ValueError, match="LANGGRAPH_DB_HOST"):
        host.create_app()


def test_langgraph_host_requires_database_username(monkeypatch):
    _configure(monkeypatch, "unused_worker_module")
    monkeypatch.delenv("LANGGRAPH_DB_USERNAME")

    with pytest.raises(ValueError, match="LANGGRAPH_DB_USERNAME"):
        host.create_app()


def test_langgraph_database_credentials_are_service_specific_and_structured(
    monkeypatch,
):
    _configure(monkeypatch, "unused_worker_module")

    parsed = make_url(postgres_url_from_environment("LANGGRAPH", required=True))

    assert parsed.username == "langgraph-user"
    assert parsed.password == "langgraph-secret"
    assert parsed.database == "langgraph"


def test_migration_admin_url_uses_structured_database_settings(monkeypatch):
    for key, value in {
        "POSTGRES_ADMIN_USERNAME": "codefactory_admin",
        "POSTGRES_ADMIN_PASSWORD": "admin:password/with?reserved",
        "GITHUB_DB_HOST": "codefactory-postgres",
        "GITHUB_DB_PORT": "5432",
        "GITHUB_DB_NAME": "codefactory",
        "GITHUB_DB_SSLMODE": "disable",
    }.items():
        monkeypatch.setenv(key, value)

    parsed = make_url(migrate._admin_url())

    assert parsed.username == "codefactory_admin"
    assert parsed.password == "admin:password/with?reserved"
    assert parsed.host == "codefactory-postgres"
    assert parsed.database == "codefactory"


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


def test_langgraph_host_uses_bundled_operations_factory_by_default(monkeypatch):
    _configure(monkeypatch, "unused_worker_module")
    monkeypatch.delenv("WORKFLOW_OPERATIONS_MODULE")

    app = host.create_app()
    operations = make_operations(github_client=object(), proposal_provider=object())

    assert any(route.path == "/healthz" for route in app.routes)
    assert all(
        callable(operation)
        for operation in (
            operations.gather_context,
            operations.create_plan,
            operations.assess_risk,
            operations.prepare_changes,
            operations.repair_changes,
        )
    )


def test_langgraph_readiness_exposes_local_model_unavailability(monkeypatch):
    _configure(monkeypatch, "unused_worker_module")
    monkeypatch.delenv("WORKFLOW_OPERATIONS_MODULE")
    app = host.create_app()
    app.state.proposal_provider = type(
        "UnavailableProvider", (), {"is_ready": lambda self: False}
    )()
    endpoint = next(route.endpoint for route in app.routes if route.path == "/readyz")

    response = asyncio.run(endpoint())

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "status": "unavailable",
        "dependency": "local_model",
    }
