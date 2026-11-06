"""Apply bundled PostgreSQL migrations before application workloads start."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import ClientCursor, sql
from sqlalchemy.engine import URL

from langgraph_service.storage.database import postgres_url_from_environment

MIGRATION_FILES = (
    "001_github_event_queue.sql",
    "001_checkpoint_schema.sql",
    "002_service_roles.sql",
    "002_agent_config.sql",
    "003_agent_roster.sql",
    "003_github_task_installation.sql",
    "004_github_project_identity.sql",
    "005_github_review_event.sql",
    "006_github_human_feedback_reply.sql",
    "007_github_agent_repair_requested.sql",
    "008_langgraph_model_profile.sql",
    "009_inference_instance_config.sql",
    "010_inference_model_downloads.sql",
    "011_inference_model_rollouts.sql",
    "012_multiple_inference_instances.sql",
    "013_global_model_downloads.sql",
    "014_inference_model_apply.sql",
    "015_inference_instance_lifecycle.sql",
)
INCREMENTAL_MIGRATION_FILES = (
    "009_inference_instance_config.sql",
    "010_inference_model_downloads.sql",
    "011_inference_model_rollouts.sql",
    "012_multiple_inference_instances.sql",
)
MULTIPLE_INFERENCE_INSTANCES_MIGRATION_FILES = ("012_multiple_inference_instances.sql",)
GLOBAL_MODEL_DOWNLOADS_MIGRATION_FILES = ("013_global_model_downloads.sql",)
INFERENCE_MODEL_APPLY_MIGRATION_FILES = ("014_inference_model_apply.sql",)
INFERENCE_INSTANCE_LIFECYCLE_MIGRATION_FILES = ("015_inference_instance_lifecycle.sql",)
MIGRATION_PROFILES = {
    "full": MIGRATION_FILES,
    "inference-009-012": INCREMENTAL_MIGRATION_FILES,
    "inference-012": MULTIPLE_INFERENCE_INSTANCES_MIGRATION_FILES,
    "inference-013": GLOBAL_MODEL_DOWNLOADS_MIGRATION_FILES,
    "inference-014": INFERENCE_MODEL_APPLY_MIGRATION_FILES,
    "inference-015": INFERENCE_INSTANCE_LIFECYCLE_MIGRATION_FILES,
}


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"{name} must be configured")
    return value


def _admin_url() -> str:
    try:
        port = int(_required("GITHUB_DB_PORT"))
    except ValueError as error:
        raise ValueError("GITHUB_DB_PORT must be an integer") from error
    if not 1 <= port <= 65535:
        raise ValueError("GITHUB_DB_PORT must be between 1 and 65535")
    return URL.create(
        drivername="postgresql",
        username=_required("POSTGRES_ADMIN_USERNAME"),
        password=_required("POSTGRES_ADMIN_PASSWORD"),
        host=_required("GITHUB_DB_HOST"),
        port=port,
        database=_required("GITHUB_DB_NAME"),
        query={"sslmode": _required("GITHUB_DB_SSLMODE")},
    ).render_as_string(hide_password=False)


def _ensure_login_role(
    connection: psycopg.Connection,
    username: str,
    password: str,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (username,))
        exists = cursor.fetchone() is not None
        action = "ALTER ROLE" if exists else "CREATE ROLE"
        cursor.execute(
            sql.SQL("{} {} LOGIN PASSWORD {}").format(
                sql.SQL(action),
                sql.Identifier(username),
                sql.Literal(password),
            )
        )


def _apply_sql_file(connection: psycopg.Connection, path: Path) -> None:
    with ClientCursor(connection) as cursor:
        cursor.execute(path.read_text(encoding="utf-8"))


def _migration_files(
    profile: str,
    migration_directory: Path,
    *,
    registered_files: tuple[str, ...] = MIGRATION_FILES,
) -> tuple[Path, ...]:
    if profile not in MIGRATION_PROFILES:
        raise ValueError(f"unsupported MIGRATION_PROFILE: {profile}")
    expected = MIGRATION_PROFILES[profile]
    registered = tuple(name for name in registered_files if name in expected)
    if registered != expected:
        raise ValueError(f"migration order is invalid for profile {profile}")
    paths = tuple(migration_directory / name for name in expected)
    missing = tuple(path.name for path in paths if not path.is_file())
    if missing:
        raise FileNotFoundError(
            f"migration files are missing for profile {profile}: {', '.join(missing)}"
        )
    return paths


def _apply_migrations(
    connection: psycopg.Connection, migrations: tuple[Path, ...]
) -> None:
    for migration_file in migrations:
        with connection.transaction():
            _apply_sql_file(connection, migration_file)


def _set_migration_ready(connection: psycopg.Connection, ready: bool) -> None:
    with connection.cursor() as cursor:
        cursor.execute("CREATE SCHEMA IF NOT EXISTS codefactory_migrations")
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS codefactory_migrations.migration_state ("
            "id SMALLINT PRIMARY KEY CHECK (id = 1), "
            "ready BOOLEAN NOT NULL DEFAULT FALSE)"
        )
        cursor.execute(
            "INSERT INTO codefactory_migrations.migration_state (id, ready) "
            "VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET ready = EXCLUDED.ready",
            (ready,),
        )


def _grant_runtime_access(connection: psycopg.Connection) -> None:
    database = _required("GITHUB_DB_NAME")
    for service, runtime_role in (
        ("GITHUB", "github_service_runtime"),
        ("LANGGRAPH", "langgraph_service_runtime"),
    ):
        username = _required(f"{service}_DB_USERNAME")
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(runtime_role), sql.Identifier(username)
                )
            )
            if service == "LANGGRAPH":
                cursor.execute(
                    sql.SQL(
                        "ALTER ROLE {} IN DATABASE {} "
                        "SET search_path TO langgraph_service"
                    ).format(sql.Identifier(username), sql.Identifier(database))
                )
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(database)
            )
        )
        cursor.execute(
            sql.SQL(
                "GRANT CONNECT ON DATABASE {} TO "
                "github_service_runtime, langgraph_service_runtime"
            ).format(sql.Identifier(database))
        )
        cursor.execute(
            "GRANT CREATE ON SCHEMA langgraph_service TO langgraph_service_runtime"
        )
        cursor.execute("REVOKE ALL ON SCHEMA codefactory_migrations FROM PUBLIC")
        cursor.execute(
            "REVOKE ALL ON TABLE codefactory_migrations.migration_state FROM PUBLIC"
        )
        cursor.execute(
            "GRANT USAGE ON SCHEMA codefactory_migrations "
            "TO github_service_runtime, langgraph_service_runtime"
        )
        cursor.execute(
            "GRANT SELECT ON TABLE codefactory_migrations.migration_state "
            "TO github_service_runtime, langgraph_service_runtime"
        )


def _revoke_checkpoint_schema_create(connection: psycopg.Connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "REVOKE CREATE ON SCHEMA langgraph_service FROM langgraph_service_runtime"
        )


def run_migrations() -> None:
    postgres_url_from_environment("GITHUB", required=True)
    langgraph_url = postgres_url_from_environment("LANGGRAPH", required=True)
    migration_directory = Path(os.environ.get("MIGRATIONS_DIR", "/migrations"))
    migration_profile = os.environ.get("MIGRATION_PROFILE", "full")
    migrations = _migration_files(migration_profile, migration_directory)

    with psycopg.connect(_admin_url(), autocommit=True) as connection:
        _ensure_login_role(
            connection,
            _required("GITHUB_DB_USERNAME"),
            _required("GITHUB_DB_PASSWORD"),
        )
        _ensure_login_role(
            connection,
            _required("LANGGRAPH_DB_USERNAME"),
            _required("LANGGRAPH_DB_PASSWORD"),
        )
        _set_migration_ready(connection, False)
        _apply_migrations(connection, migrations)
        _grant_runtime_access(connection)

    checkpoint_setup_complete = False
    try:
        with PostgresSaver.from_conn_string(langgraph_url) as checkpointer:
            checkpointer.setup()
        checkpoint_setup_complete = True
    finally:
        with psycopg.connect(_admin_url(), autocommit=True) as connection:
            _revoke_checkpoint_schema_create(connection)
            if checkpoint_setup_complete:
                _set_migration_ready(connection, True)


def main() -> None:
    run_migrations()


if __name__ == "__main__":
    main()
