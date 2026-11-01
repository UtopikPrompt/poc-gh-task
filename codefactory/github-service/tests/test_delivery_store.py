import sqlite3

import pytest

from github_service.delivery_store import (
    PostgresDeliveryStore,
    SQLiteDeliveryStore,
)


def test_sqlite_queue_is_idempotent_and_requires_current_claim_token(tmp_path):
    store = SQLiteDeliveryStore(tmp_path / "github.sqlite")
    try:
        assert store.claim_delivery("delivery-1") == "claimed"
        event = {"task_id": "task-1", "event_type": "ready"}
        first = store.enqueue_events("delivery-1", [event])
        assert store.enqueue_events("delivery-1", [event]) == first
        store.complete_delivery("delivery-1", {"status": "queued"})
        assert store.claim_delivery("delivery-1") == "duplicate"

        claim = store.claim_event()
        assert claim is not None
        assert claim["event_id"] == first[0]
        assert claim["payload"] == event
        assert store.acknowledge_event(claim["event_id"], "stale-token") is False
        assert store.acknowledge_event(claim["event_id"], claim["claim_token"]) is True
        assert store.acknowledge_event(claim["event_id"], claim["claim_token"]) is False
        assert store.claim_event() is None
    finally:
        store.close()


def test_sqlite_queue_retries_and_dead_letters_at_attempt_limit(tmp_path):
    store = SQLiteDeliveryStore(tmp_path / "github.sqlite")
    try:
        store.claim_delivery("delivery-2")
        event_id = store.enqueue_events(
            "delivery-2", [{"task_id": "task-2", "event_type": "ci"}]
        )[0]
        store.complete_delivery("delivery-2", {"status": "queued"})
        first = store.claim_event(max_attempts=2)
        assert first is not None and first["attempt"] == 1
        assert store.retry_event(
            event_id, first["claim_token"], "transient", max_attempts=2
        )
        assert store.acknowledge_event(event_id, first["claim_token"]) is False

        store._connection.execute(
            "UPDATE task_event_outbox SET available_at = datetime('now', '-1 second') "
            "WHERE event_id = ?",
            (event_id,),
        )
        second = store.claim_event(max_attempts=2)
        assert second is not None and second["attempt"] == 2
        assert store.retry_event(
            event_id, second["claim_token"], "permanent", max_attempts=2
        )
        assert store.claim_event(max_attempts=2) is None
        row = store._connection.execute(
            "SELECT state, attempts, last_error FROM task_event_outbox "
            "WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        assert tuple(row) == ("dead", 2, "permanent")
    finally:
        store.close()


def test_expired_lease_at_attempt_limit_is_marked_dead(tmp_path):
    store = SQLiteDeliveryStore(tmp_path / "github.sqlite")
    try:
        store.claim_delivery("delivery-expired")
        event_id = store.enqueue_events(
            "delivery-expired", [{"task_id": "task-expired", "event_type": "ci"}]
        )[0]
        first = store.claim_event(lease_seconds=0, max_attempts=1)
        assert first is not None
        assert store.claim_event(max_attempts=1) is None
        state = store._connection.execute(
            "SELECT state, last_error FROM task_event_outbox WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        assert tuple(state) == ("dead", "lease expired after maximum attempts")
    finally:
        store.close()


def test_queue_serializes_events_for_the_same_task(tmp_path):
    store = SQLiteDeliveryStore(tmp_path / "github.sqlite")
    try:
        store.claim_delivery("delivery-ordered")
        store.enqueue_events(
            "delivery-ordered",
            [
                {"task_id": "task-ordered", "event_type": "ready"},
                {"task_id": "task-ordered", "event_type": "ci"},
            ],
        )
        store.complete_delivery("delivery-ordered", {"status": "queued"})

        first = store.claim_event()
        assert first is not None
        assert first["payload"]["event_type"] == "ready"
        assert store.claim_event() is None
        assert store.acknowledge_event(first["event_id"], first["claim_token"])

        second = store.claim_event()
        assert second is not None
        assert second["payload"]["event_type"] == "ci"
    finally:
        store.close()


def test_task_installation_context_is_persisted_and_cannot_be_switched(tmp_path):
    store = SQLiteDeliveryStore(tmp_path / "github.sqlite")
    ready_event = {
        "task_id": "task-installation",
        "event_type": "ready",
        "project_node_id": "PVT_project",
        "project_item_id": "PVTI_item",
        "target_repositories": ["example/project"],
        "installation_id": 654,
    }
    try:
        store.enqueue_events("delivery-first", [ready_event])
        assert store.task_context("task-installation") == {
            "project_node_id": "PVT_project",
            "project_item_id": "PVTI_item",
            "target_repository": "example/project",
            "installation_id": "654",
        }

        conflicting_event = {**ready_event, "installation_id": 987}
        with pytest.raises(ValueError, match="immutable task context"):
            store.enqueue_events("delivery-conflict", [conflicting_event])
        assert store.task_context("task-installation")["installation_id"] == "654"
    finally:
        store.close()


def test_legacy_task_context_without_installation_is_not_reused(tmp_path):
    database_path = tmp_path / "github.sqlite"
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE task_contexts (task_id TEXT PRIMARY KEY, "
        "project_node_id TEXT NOT NULL, project_item_id TEXT NOT NULL, "
        "target_repository TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO task_contexts VALUES (?, ?, ?, ?)",
        ("legacy-task", "PVT_project", "PVTI_item", "example/project"),
    )
    connection.commit()
    connection.close()

    store = SQLiteDeliveryStore(database_path)
    try:
        assert store.task_context("legacy-task") is None
        assert "installation_id" in {
            row["name"]
            for row in store._connection.execute("PRAGMA table_info(task_contexts)")
        }
    finally:
        store.close()


def test_postgres_claim_uses_skip_locked_and_returns_claimed_event():
    statements = []

    class Cursor:
        rowcount = 1

        def __init__(self):
            self.result = None

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def execute(self, statement, params=()):
            statements.append((statement, params))
            if "SKIP LOCKED" in statement:
                self.result = {
                    "event_id": "event-1",
                    "attempts": 1,
                    "claim_token": "claim-1",
                }
            elif "SELECT delivery_id, payload" in statement:
                self.result = {
                    "delivery_id": "delivery-1",
                    "payload": {"task_id": "task-1", "event_type": "ready"},
                }

        def fetchone(self):
            return self.result

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    store = PostgresDeliveryStore("postgresql://test", connection_factory=Connection)
    claim = store.claim_event()

    assert claim == {
        "event_id": "event-1",
        "delivery_id": "delivery-1",
        "claim_token": "claim-1",
        "attempt": 1,
        "payload": {"task_id": "task-1", "event_type": "ready"},
    }
    assert "FOR UPDATE OF candidate_outbox SKIP LOCKED" in statements[1][0]
    assert "attempts < %s" in statements[1][0]
    assert "pg_try_advisory_xact_lock" in statements[1][0]


def test_postgres_concurrent_webhook_delivery_reports_in_progress():
    statements = []

    class Cursor:
        result = None

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def execute(self, statement, params=()):
            statements.append(statement)
            if statement.startswith("INSERT"):
                self.result = None
            elif statement.startswith("SELECT state"):
                self.result = {"state": "processing", "in_progress": True}

        def fetchone(self):
            return self.result

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    store = PostgresDeliveryStore("postgresql://test", connection_factory=Connection)

    assert store.claim_delivery("delivery-1") == "in_progress"
    assert len(statements) == 2
    assert "FOR UPDATE" in statements[1]


@pytest.mark.parametrize("stored_installation_id", [654, 987])
def test_postgres_task_context_never_changes_installation(stored_installation_id):
    statements = []
    connection_state = {"committed": False, "rolled_back": False}

    class Cursor:
        def __init__(self):
            self.result = None

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def execute(self, statement, params=()):
            statements.append((statement, params))
            if "INSERT INTO github_service.task_event_inbox" in statement:
                self.result = {"event_id": "event-1"}
            elif "SELECT project_node_id, project_item_id" in statement:
                self.result = {
                    "project_node_id": "PVT_project",
                    "project_item_id": "PVTI_item",
                    "target_repository": "example/project",
                    "installation_id": stored_installation_id,
                }
            else:
                self.result = None

        def fetchone(self):
            return self.result

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            connection_state["committed"] = True

        def rollback(self):
            connection_state["rolled_back"] = True

        def close(self):
            pass

    store = PostgresDeliveryStore(
        "postgresql://test",
        connection_factory=Connection,
    )
    event = {
        "task_id": "task-1",
        "event_type": "ready",
        "project_node_id": "PVT_project",
        "project_item_id": "PVTI_item",
        "target_repositories": ["example/project"],
        "installation_id": 654,
    }

    if stored_installation_id == 654:
        assert store.enqueue_events("delivery-1", [event]) == ["event-1"]
        assert connection_state == {"committed": True, "rolled_back": False}
    else:
        with pytest.raises(ValueError, match="immutable task context"):
            store.enqueue_events("delivery-1", [event])
        assert connection_state == {"committed": False, "rolled_back": True}

    context_insert = next(
        statement
        for statement, _ in statements
        if "INSERT INTO github_service.task_contexts" in statement
    )
    assert "ON CONFLICT(task_id) DO NOTHING" in context_insert
    assert "DO UPDATE" not in context_insert
