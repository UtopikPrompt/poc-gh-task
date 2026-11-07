from pathlib import Path
from unittest.mock import MagicMock

import pytest

from langgraph_service.storage.migrate import (
    GLOBAL_MODEL_DOWNLOADS_MIGRATION_FILES,
    INFERENCE_INSTANCE_LIFECYCLE_MIGRATION_FILES,
    INFERENCE_MODEL_APPLY_MIGRATION_FILES,
    INFERENCE_WORKLOAD_LIFECYCLE_SETUP_MIGRATION_FILES,
    INFERENCE_WORKLOAD_LIFECYCLE_MIGRATION_FILES,
    INCREMENTAL_MIGRATION_FILES,
    MIGRATION_FILES,
    MULTIPLE_INFERENCE_INSTANCES_MIGRATION_FILES,
    _apply_migrations,
    _migration_files,
)


def _migration_directory(tmp_path, filenames):
    for filename in filenames:
        (tmp_path / filename).touch()
    return tmp_path


def test_full_profile_selects_all_migrations_in_fresh_install_order(tmp_path):
    directory = _migration_directory(tmp_path, MIGRATION_FILES)

    assert tuple(path.name for path in _migration_files("full", directory)) == (
        MIGRATION_FILES
    )


def test_incremental_profile_selects_only_migrations_009_through_012(tmp_path):
    directory = _migration_directory(tmp_path, MIGRATION_FILES)

    assert (
        tuple(path.name for path in _migration_files("inference-009-012", directory))
        == INCREMENTAL_MIGRATION_FILES
    )


def test_multiple_instance_profile_selects_only_migration_012(tmp_path):
    directory = _migration_directory(tmp_path, MIGRATION_FILES)

    assert tuple(
        path.name for path in _migration_files("inference-012", directory)
    ) == (MULTIPLE_INFERENCE_INSTANCES_MIGRATION_FILES)


def test_inference_workload_lifecycle_profile_selects_only_migration_016(tmp_path):
    directory = _migration_directory(tmp_path, MIGRATION_FILES)

    assert (
        tuple(path.name for path in _migration_files("inference-016", directory))
        == INFERENCE_WORKLOAD_LIFECYCLE_MIGRATION_FILES
    )


def test_inference_workload_lifecycle_setup_profile_selects_migrations_015_then_016(
    tmp_path,
):
    directory = _migration_directory(tmp_path, MIGRATION_FILES)

    assert (
        tuple(path.name for path in _migration_files("inference-015-016", directory))
        == INFERENCE_WORKLOAD_LIFECYCLE_SETUP_MIGRATION_FILES
    )


def test_inference_runtime_options_profile_selects_only_migration_017(tmp_path):
    directory = _migration_directory(tmp_path, MIGRATION_FILES)

    assert tuple(
        path.name for path in _migration_files("inference-017", directory)
    ) == ("017_inference_runtime_options.sql",)


def test_fresh_inference_configuration_migration_does_not_seed_instances():
    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "009_inference_instance_config.sql"
    )

    assert "INSERT INTO langgraph_service.inference_instance_config" not in (
        migration.read_text(encoding="utf-8")
    )


def test_global_model_downloads_profile_selects_only_migration_013(tmp_path):
    directory = _migration_directory(tmp_path, MIGRATION_FILES)

    assert (
        tuple(path.name for path in _migration_files("inference-013", directory))
        == GLOBAL_MODEL_DOWNLOADS_MIGRATION_FILES
    )


def test_inference_model_apply_profile_selects_only_migration_014(tmp_path):
    directory = _migration_directory(tmp_path, MIGRATION_FILES)

    assert (
        tuple(path.name for path in _migration_files("inference-014", directory))
        == INFERENCE_MODEL_APPLY_MIGRATION_FILES
    )


def test_inference_instance_lifecycle_profile_selects_only_migration_015(tmp_path):
    directory = _migration_directory(tmp_path, MIGRATION_FILES)

    assert (
        tuple(path.name for path in _migration_files("inference-015", directory))
        == INFERENCE_INSTANCE_LIFECYCLE_MIGRATION_FILES
    )


def test_migration_profile_fails_before_execution_if_a_file_is_missing(tmp_path):
    directory = _migration_directory(tmp_path, INCREMENTAL_MIGRATION_FILES[:-1])

    with pytest.raises(FileNotFoundError, match="012_multiple_inference_instances.sql"):
        _migration_files("inference-009-012", directory)


def test_migration_profile_rejects_an_invalid_registered_order(tmp_path):
    directory = _migration_directory(tmp_path, INCREMENTAL_MIGRATION_FILES)

    with pytest.raises(ValueError, match="migration order is invalid"):
        _migration_files(
            "inference-009-012",
            directory,
            registered_files=tuple(reversed(INCREMENTAL_MIGRATION_FILES)),
        )


def test_each_migration_is_applied_in_its_own_transaction(monkeypatch, tmp_path):
    migrations = tuple(tmp_path / filename for filename in INCREMENTAL_MIGRATION_FILES)
    applied = []
    monkeypatch.setattr(
        "langgraph_service.storage.migrate._apply_sql_file",
        lambda _connection, path: applied.append(path),
    )
    connection = MagicMock()

    _apply_migrations(connection, migrations)

    assert connection.transaction.call_count == len(migrations)
    assert applied == list(migrations)


def test_human_feedback_reply_migration_follows_review_event_migration():
    review_event_index = MIGRATION_FILES.index("005_github_review_event.sql")

    assert (
        MIGRATION_FILES[review_event_index + 1] == "006_github_human_feedback_reply.sql"
    )


def test_agent_repair_request_migration_follows_feedback_reply_migration():
    feedback_reply_index = MIGRATION_FILES.index("006_github_human_feedback_reply.sql")

    assert (
        MIGRATION_FILES[feedback_reply_index + 1]
        == "007_github_agent_repair_requested.sql"
    )


def test_inference_instance_and_rollout_migrations_are_registered_in_order():
    assert MIGRATION_FILES[-10:] == (
        "008_langgraph_model_profile.sql",
        "009_inference_instance_config.sql",
        "010_inference_model_downloads.sql",
        "011_inference_model_rollouts.sql",
        "012_multiple_inference_instances.sql",
        "013_global_model_downloads.sql",
        "014_inference_model_apply.sql",
        "015_inference_instance_lifecycle.sql",
        "016_inference_workload_lifecycle.sql",
        "017_inference_runtime_options.sql",
    )
