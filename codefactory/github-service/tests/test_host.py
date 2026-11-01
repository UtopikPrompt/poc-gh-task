from github_service import bootstrap, host
from github_service.settings import RuntimeSettings


def test_github_host_requires_production_postgres(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.delenv("GITHUB_DATABASE_URL", raising=False)

    try:
        host.create_app()
    except ValueError as error:
        assert "GITHUB_DATABASE_URL" in str(error)
    else:
        raise AssertionError("production GitHub host must require PostgreSQL")


def test_github_host_does_not_load_langgraph_operations(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("GITHUB_DATABASE_URL", "postgresql://github-service")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "configured-token")
    monkeypatch.setenv("GITHUB_PROJECT_READY_OPTION_ID", "option-ready")
    monkeypatch.delenv("WORKFLOW_OPERATIONS_MODULE", raising=False)
    received = []
    monkeypatch.setattr(
        bootstrap,
        "create_github_app",
        lambda *, settings: received.append(settings) or "github-app",
    )

    assert host.create_app() == "github-app"
    assert received[0].github_database_url == "postgresql://github-service"


def test_runtime_settings_reject_nonpositive_worker_lease(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("WORKER_LEASE_SECONDS", "0")

    try:
        RuntimeSettings.from_environment()
    except ValueError as error:
        assert "worker poll and lease settings" in str(error)
    else:
        raise AssertionError("worker lease must be positive")
