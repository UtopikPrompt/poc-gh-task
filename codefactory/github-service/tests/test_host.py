from github_service import bootstrap, host
from github_service.database import postgres_url_from_environment
from github_service.settings import RuntimeSettings
from sqlalchemy.engine import make_url


def _set_github_database_settings(monkeypatch):
    for key, value in {
        "GITHUB_DB_HOST": "postgres.example.test",
        "GITHUB_DB_PORT": "5432",
        "GITHUB_DB_NAME": "github/db",
        "GITHUB_DB_USERNAME": "github-user",
        "GITHUB_DB_PASSWORD": "p@ss:/word%",
        "GITHUB_DB_SSLMODE": "verify-full",
    }.items():
        monkeypatch.setenv(key, value)


def test_github_host_requires_production_postgres(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "webhook-secret")
    for suffix in ("HOST", "PORT", "NAME", "USERNAME", "PASSWORD", "SSLMODE"):
        monkeypatch.delenv(f"GITHUB_DB_{suffix}", raising=False)

    try:
        host.create_app()
    except ValueError as error:
        assert "GITHUB_DB_*" in str(error)
    else:
        raise AssertionError("production GitHub host must require PostgreSQL")


def test_github_host_does_not_fallback_to_database_url(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("GITHUB_DATABASE_URL", "postgresql://legacy/db")
    for suffix in ("HOST", "PORT", "NAME", "USERNAME", "PASSWORD", "SSLMODE"):
        monkeypatch.delenv(f"GITHUB_DB_{suffix}", raising=False)

    try:
        host.create_app()
    except ValueError as error:
        assert "GITHUB_DB_*" in str(error)
    else:
        raise AssertionError("legacy full URL must not configure production PostgreSQL")


def test_github_host_requires_database_username(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "webhook-secret")
    _set_github_database_settings(monkeypatch)
    monkeypatch.delenv("GITHUB_DB_USERNAME")

    try:
        host.create_app()
    except ValueError as error:
        assert "GITHUB_DB_USERNAME" in str(error)
    else:
        raise AssertionError("GitHub database username must be configured")


def test_github_host_does_not_load_langgraph_operations(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "webhook-secret")
    _set_github_database_settings(monkeypatch)
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "configured-token")
    monkeypatch.setenv("GITHUB_APP_ID", "321")
    monkeypatch.setenv("GITHUB_PROJECT_OWNER", "example-org")
    monkeypatch.setenv("GITHUB_PROJECT_OWNER_TYPE", "organization")
    monkeypatch.setenv("GITHUB_PROJECT_NUMBER", "7")
    monkeypatch.delenv("WORKFLOW_OPERATIONS_MODULE", raising=False)
    received = []
    monkeypatch.setattr(
        bootstrap,
        "create_github_app",
        lambda *, settings: received.append(settings) or "github-app",
    )

    assert host.create_app() == "github-app"
    assert make_url(received[0].github_database_url).password == "p@ss:/word%"


def test_postgres_url_encodes_credentials_and_database_name(monkeypatch):
    _set_github_database_settings(monkeypatch)

    url = postgres_url_from_environment("GITHUB", required=True)
    parsed = make_url(url)

    assert parsed.username == "github-user"
    assert parsed.password == "p@ss:/word%"
    assert parsed.database == "github/db"
    assert parsed.query == {"sslmode": "verify-full"}
    assert "p%40ss%3A%2Fword%25" in url


def test_runtime_settings_reject_nonpositive_worker_lease(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("WORKER_LEASE_SECONDS", "0")

    try:
        RuntimeSettings.from_environment()
    except ValueError as error:
        assert "worker poll and lease settings" in str(error)
    else:
        raise AssertionError("worker lease must be positive")
