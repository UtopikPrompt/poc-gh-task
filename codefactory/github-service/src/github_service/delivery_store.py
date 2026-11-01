"""Durable webhook ledger and task-event queue storage."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

DeliveryClaim = Literal["claimed", "duplicate", "in_progress"]


class SQLiteDeliveryStore:
    """Local-only implementation used by tests and developer runs."""

    def __init__(self, database_path: Path):
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            database_path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS webhook_deliveries (
                delivery_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                response_json TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS task_event_inbox (
                event_id TEXT PRIMARY KEY,
                delivery_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                task_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (delivery_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS task_event_outbox (
                event_id TEXT PRIMARY KEY REFERENCES task_event_inbox(event_id),
                state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                lease_until TEXT,
                claim_token TEXT,
                last_error TEXT,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS task_contexts (
                task_id TEXT PRIMARY KEY,
                project_node_id TEXT NOT NULL,
                project_item_id TEXT NOT NULL,
                target_repository TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS task_event_outbox_ready
                ON task_event_outbox(state, available_at, lease_until);
            CREATE TABLE IF NOT EXISTS pull_request_tasks (
                repository TEXT NOT NULL,
                pull_request_number INTEGER NOT NULL,
                pull_request_id TEXT,
                task_id TEXT NOT NULL UNIQUE,
                PRIMARY KEY (repository, pull_request_number)
            );
            """
        )

    def claim_delivery(self, delivery_id: str) -> DeliveryClaim:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT state, updated_at FROM webhook_deliveries "
                    "WHERE delivery_id = ?",
                    (delivery_id,),
                ).fetchone()
                if row is None:
                    self._connection.execute(
                        "INSERT INTO webhook_deliveries (delivery_id, state) "
                        "VALUES (?, 'processing')",
                        (delivery_id,),
                    )
                    claim: DeliveryClaim = "claimed"
                elif row["state"] in {"accepted", "processed"}:
                    claim = "duplicate"
                elif (
                    row["state"] == "processing"
                    and self._connection.execute(
                        "SELECT datetime(?) > datetime('now', '-900 seconds')",
                        (row["updated_at"],),
                    ).fetchone()[0]
                ):
                    claim = "in_progress"
                else:
                    self._connection.execute(
                        "UPDATE webhook_deliveries SET state = 'processing', "
                        "updated_at = CURRENT_TIMESTAMP WHERE delivery_id = ?",
                        (delivery_id,),
                    )
                    claim = "claimed"
                self._connection.commit()
                return claim
            except Exception:
                self._connection.rollback()
                raise

    def enqueue_events(
        self,
        delivery_id: str,
        events: Sequence[Mapping[str, object]],
    ) -> list[str]:
        event_ids: list[str] = []
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                for ordinal, event in enumerate(events):
                    event_id = str(uuid.uuid4())
                    self._connection.execute(
                        "INSERT OR IGNORE INTO task_event_inbox "
                        "(event_id, delivery_id, ordinal, task_id, event_type, "
                        "payload_json) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            event_id,
                            delivery_id,
                            ordinal,
                            str(event["task_id"]),
                            str(event["event_type"]),
                            json.dumps(dict(event)),
                        ),
                    )
                    row = self._connection.execute(
                        "SELECT event_id FROM task_event_inbox "
                        "WHERE delivery_id = ? AND ordinal = ?",
                        (delivery_id, ordinal),
                    ).fetchone()
                    event_id = str(row["event_id"])
                    self._connection.execute(
                        "INSERT OR IGNORE INTO task_event_outbox (event_id) VALUES (?)",
                        (event_id,),
                    )
                    project_node_id = event.get("project_node_id")
                    project_item_id = event.get("project_item_id")
                    repositories = event.get("target_repositories")
                    if (
                        event.get("event_type") == "ready"
                        and isinstance(project_node_id, str)
                        and isinstance(project_item_id, str)
                        and isinstance(repositories, list)
                        and len(repositories) == 1
                        and isinstance(repositories[0], str)
                    ):
                        self._connection.execute(
                            "INSERT INTO task_contexts "
                            "(task_id, project_node_id, project_item_id, "
                            "target_repository) VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(task_id) DO UPDATE SET "
                            "project_node_id = excluded.project_node_id, "
                            "project_item_id = excluded.project_item_id, "
                            "target_repository = excluded.target_repository",
                            (
                                str(event["task_id"]),
                                project_node_id,
                                project_item_id,
                                repositories[0],
                            ),
                        )
                    event_ids.append(event_id)
                self._connection.commit()
                return event_ids
            except Exception:
                self._connection.rollback()
                raise

    def complete_delivery(
        self,
        delivery_id: str,
        response: dict[str, object],
    ) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE webhook_deliveries SET state = 'accepted', response_json = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE delivery_id = ?",
                (json.dumps(response), delivery_id),
            )

    def fail_delivery(self, delivery_id: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE webhook_deliveries SET state = 'failed', "
                "updated_at = CURRENT_TIMESTAMP WHERE delivery_id = ?",
                (delivery_id,),
            )

    def claim_event(
        self,
        *,
        lease_seconds: int = 60,
        max_attempts: int = 8,
    ) -> dict[str, object] | None:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    "UPDATE task_event_outbox SET state = 'dead', "
                    "last_error = 'lease expired after maximum attempts' "
                    "WHERE state = 'processing' AND attempts >= ? "
                    "AND lease_until <= CURRENT_TIMESTAMP",
                    (max_attempts,),
                )
                row = self._connection.execute(
                    "SELECT o.event_id, o.attempts, i.delivery_id, i.payload_json "
                    "FROM task_event_outbox o JOIN task_event_inbox i USING(event_id) "
                    "WHERE o.attempts < ? AND o.available_at <= CURRENT_TIMESTAMP "
                    "AND (o.state = 'pending' OR "
                    "(o.state = 'processing' AND o.lease_until <= CURRENT_TIMESTAMP)) "
                    "AND NOT EXISTS (SELECT 1 FROM task_event_outbox active "
                    "JOIN task_event_inbox active_event USING(event_id) "
                    "WHERE active_event.task_id = i.task_id "
                    "AND active.state = 'processing' "
                    "AND active.lease_until > CURRENT_TIMESTAMP "
                    "AND active.event_id != o.event_id) "
                    "ORDER BY i.created_at, i.ordinal LIMIT 1",
                    (max_attempts,),
                ).fetchone()
                if row is None:
                    self._connection.commit()
                    return None
                claim_token = str(uuid.uuid4())
                self._connection.execute(
                    "UPDATE task_event_outbox SET state = 'processing', "
                    "attempts = attempts + 1, claim_token = ?, "
                    "lease_until = datetime('now', ?), last_error = NULL "
                    "WHERE event_id = ?",
                    (claim_token, f"+{lease_seconds} seconds", row["event_id"]),
                )
                self._connection.commit()
                return {
                    "event_id": str(row["event_id"]),
                    "delivery_id": str(row["delivery_id"]),
                    "claim_token": claim_token,
                    "attempt": int(row["attempts"]) + 1,
                    "payload": json.loads(row["payload_json"]),
                }
            except Exception:
                self._connection.rollback()
                raise

    def acknowledge_event(self, event_id: str, claim_token: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE task_event_outbox SET state = 'processed', "
                "completed_at = CURRENT_TIMESTAMP, lease_until = NULL "
                "WHERE event_id = ? AND state = 'processing' AND claim_token = ?",
                (event_id, claim_token),
            )
            return cursor.rowcount == 1

    def retry_event(
        self,
        event_id: str,
        claim_token: str,
        error: str,
        *,
        max_attempts: int = 8,
    ) -> bool:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT attempts FROM task_event_outbox "
                    "WHERE event_id = ? AND state = 'processing' AND claim_token = ?",
                    (event_id, claim_token),
                ).fetchone()
                if row is None:
                    self._connection.commit()
                    return False
                attempts = int(row["attempts"])
                state = "dead" if attempts >= max_attempts else "pending"
                delay = min(300, 2 ** min(attempts, 8))
                self._connection.execute(
                    "UPDATE task_event_outbox SET state = ?, last_error = ?, "
                    "available_at = datetime('now', ?), lease_until = NULL "
                    "WHERE event_id = ? AND claim_token = ?",
                    (state, error[:2000], f"+{delay} seconds", event_id, claim_token),
                )
                self._connection.commit()
                return True
            except Exception:
                self._connection.rollback()
                raise

    def register_pull_request(
        self,
        repository: str,
        pull_request_number: int,
        task_id: str,
        pull_request_id: str | None = None,
    ) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO pull_request_tasks "
                "(repository, pull_request_number, pull_request_id, task_id) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(repository, pull_request_number) "
                "DO UPDATE SET pull_request_id = excluded.pull_request_id, "
                "task_id = excluded.task_id",
                (repository.lower(), pull_request_number, pull_request_id, task_id),
            )

    def task_for_pull_request(
        self,
        repository: str,
        pull_request_number: int,
    ) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT task_id FROM pull_request_tasks "
                "WHERE repository = ? AND pull_request_number = ?",
                (repository.lower(), pull_request_number),
            ).fetchone()
        return None if row is None else str(row["task_id"])

    def pull_request_for_task(self, task_id: str) -> dict[str, object] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT repository, pull_request_number, pull_request_id "
                "FROM pull_request_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "repository": str(row["repository"]),
            "number": int(row["pull_request_number"]),
            "pull_request_id": row["pull_request_id"],
        }

    def task_context(self, task_id: str) -> dict[str, str] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT project_node_id, project_item_id, target_repository "
                "FROM task_contexts "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "project_node_id": str(row["project_node_id"]),
            "project_item_id": str(row["project_item_id"]),
            "target_repository": str(row["target_repository"]),
        }

    @contextmanager
    def publication_lock(self, task_id: str):
        with self._lock:
            yield

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class PostgresDeliveryStore:
    """Production PostgreSQL implementation; schema comes from migrations."""

    def __init__(
        self,
        database_url: str,
        *,
        connection_factory: Callable[..., Any] | None = None,
    ):
        self._database_url = database_url
        if connection_factory is None:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as error:
                raise RuntimeError(
                    "PostgreSQL storage requires psycopg; install the GitHub service requirements.txt"
                ) from error

            def connection_factory():
                return psycopg.connect(
                    database_url,
                    row_factory=dict_row,
                )

        self._connection_factory = connection_factory

    @contextmanager
    def _connection(self):
        connection = self._connection_factory()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_delivery(self, delivery_id: str) -> DeliveryClaim:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO github_service.webhook_deliveries "
                "(delivery_id, state) VALUES (%s, 'processing') "
                "ON CONFLICT (delivery_id) DO NOTHING RETURNING delivery_id",
                (delivery_id,),
            )
            if cursor.fetchone() is not None:
                return "claimed"
            cursor.execute(
                "SELECT state, updated_at, "
                "(state = 'processing' AND updated_at > "
                "CURRENT_TIMESTAMP - INTERVAL '15 minutes') AS in_progress "
                "FROM github_service.webhook_deliveries "
                "WHERE delivery_id = %s FOR UPDATE",
                (delivery_id,),
            )
            row = cursor.fetchone()
            if row["state"] in {"accepted", "processed"}:
                return "duplicate"
            if row["in_progress"]:
                return "in_progress"
            cursor.execute(
                "UPDATE github_service.webhook_deliveries SET state = 'processing', "
                "updated_at = CURRENT_TIMESTAMP WHERE delivery_id = %s",
                (delivery_id,),
            )
            return "claimed"

    def enqueue_events(
        self,
        delivery_id: str,
        events: Sequence[Mapping[str, object]],
    ) -> list[str]:
        event_ids: list[str] = []
        with self._connection() as connection, connection.cursor() as cursor:
            for ordinal, event in enumerate(events):
                event_id = str(uuid.uuid4())
                cursor.execute(
                    "INSERT INTO github_service.task_event_inbox "
                    "(event_id, delivery_id, ordinal, task_id, event_type, payload) "
                    "VALUES (%s, %s, %s, %s, %s, %s::jsonb) "
                    "ON CONFLICT (delivery_id, ordinal) DO NOTHING "
                    "RETURNING event_id",
                    (
                        event_id,
                        delivery_id,
                        ordinal,
                        str(event["task_id"]),
                        str(event["event_type"]),
                        json.dumps(dict(event)),
                    ),
                )
                inserted = cursor.fetchone()
                if inserted is None:
                    cursor.execute(
                        "SELECT event_id FROM github_service.task_event_inbox "
                        "WHERE delivery_id = %s AND ordinal = %s",
                        (delivery_id, ordinal),
                    )
                    inserted = cursor.fetchone()
                event_id = str(inserted["event_id"])
                cursor.execute(
                    "INSERT INTO github_service.task_event_outbox(event_id) "
                    "VALUES (%s) ON CONFLICT (event_id) DO NOTHING",
                    (event_id,),
                )
                project_node_id = event.get("project_node_id")
                project_item_id = event.get("project_item_id")
                repositories = event.get("target_repositories")
                if (
                    event.get("event_type") == "ready"
                    and isinstance(project_node_id, str)
                    and isinstance(project_item_id, str)
                    and isinstance(repositories, list)
                    and len(repositories) == 1
                    and isinstance(repositories[0], str)
                ):
                    cursor.execute(
                        "INSERT INTO github_service.task_contexts "
                        "(task_id, project_node_id, project_item_id, "
                        "target_repository) VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT(task_id) DO UPDATE SET "
                        "project_node_id = EXCLUDED.project_node_id, "
                        "project_item_id = EXCLUDED.project_item_id, "
                        "target_repository = EXCLUDED.target_repository",
                        (
                            str(event["task_id"]),
                            project_node_id,
                            project_item_id,
                            repositories[0],
                        ),
                    )
                event_ids.append(event_id)
        return event_ids

    def complete_delivery(
        self,
        delivery_id: str,
        response: dict[str, object],
    ) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE github_service.webhook_deliveries SET state = 'accepted', "
                "response = %s::jsonb, updated_at = CURRENT_TIMESTAMP "
                "WHERE delivery_id = %s",
                (json.dumps(response), delivery_id),
            )

    def fail_delivery(self, delivery_id: str) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE github_service.webhook_deliveries SET state = 'failed', "
                "updated_at = CURRENT_TIMESTAMP WHERE delivery_id = %s",
                (delivery_id,),
            )

    def claim_event(
        self,
        *,
        lease_seconds: int = 60,
        max_attempts: int = 8,
    ) -> dict[str, object] | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE github_service.task_event_outbox SET state = 'dead', "
                "last_error = 'lease expired after maximum attempts' "
                "WHERE state = 'processing' AND attempts >= %s "
                "AND lease_until <= CURRENT_TIMESTAMP",
                (max_attempts,),
            )
            cursor.execute(
                "WITH candidate AS ("
                " SELECT candidate_outbox.event_id"
                " FROM github_service.task_event_outbox candidate_outbox"
                " JOIN github_service.task_event_inbox candidate_event"
                " USING(event_id)"
                " WHERE candidate_outbox.attempts < %s"
                " AND candidate_outbox.available_at <= CURRENT_TIMESTAMP"
                " AND (candidate_outbox.state = 'pending' OR"
                " (candidate_outbox.state = 'processing' AND"
                " candidate_outbox.lease_until <= CURRENT_TIMESTAMP))"
                " AND pg_try_advisory_xact_lock("
                "hashtextextended(candidate_event.task_id, 0))"
                " AND NOT EXISTS (SELECT 1"
                " FROM github_service.task_event_outbox active"
                " JOIN github_service.task_event_inbox active_event"
                " USING(event_id)"
                " WHERE active_event.task_id = candidate_event.task_id"
                " AND active.state = 'processing'"
                " AND active.lease_until > CURRENT_TIMESTAMP"
                " AND active.event_id != candidate_outbox.event_id)"
                " ORDER BY candidate_outbox.available_at, candidate_outbox.event_id"
                " LIMIT 1 FOR UPDATE OF candidate_outbox SKIP LOCKED"
                ") UPDATE github_service.task_event_outbox AS outbox"
                " SET state = 'processing', attempts = outbox.attempts + 1,"
                " claim_token = %s, lease_until = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),"
                " last_error = NULL FROM candidate"
                " WHERE outbox.event_id = candidate.event_id"
                " RETURNING outbox.event_id, outbox.attempts, outbox.claim_token",
                (max_attempts, str(uuid.uuid4()), lease_seconds),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute(
                "SELECT delivery_id, payload FROM github_service.task_event_inbox "
                "WHERE event_id = %s",
                (row["event_id"],),
            )
            event = cursor.fetchone()
            return {
                "event_id": str(row["event_id"]),
                "delivery_id": str(event["delivery_id"]),
                "claim_token": str(row["claim_token"]),
                "attempt": int(row["attempts"]),
                "payload": event["payload"],
            }

    def acknowledge_event(self, event_id: str, claim_token: str) -> bool:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE github_service.task_event_outbox SET state = 'processed', "
                "completed_at = CURRENT_TIMESTAMP, lease_until = NULL "
                "WHERE event_id = %s AND state = 'processing' AND claim_token = %s",
                (event_id, claim_token),
            )
            return cursor.rowcount == 1

    def retry_event(
        self,
        event_id: str,
        claim_token: str,
        error: str,
        *,
        max_attempts: int = 8,
    ) -> bool:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE github_service.task_event_outbox SET state = "
                "CASE WHEN attempts >= %s THEN 'dead' ELSE 'pending' END, "
                "last_error = %s, available_at = CURRENT_TIMESTAMP + "
                "(LEAST(300, power(2, LEAST(attempts, 8)))::double precision "
                "* INTERVAL '1 second'), "
                "lease_until = NULL WHERE event_id = %s AND state = 'processing' "
                "AND claim_token = %s",
                (max_attempts, error[:2000], event_id, claim_token),
            )
            return cursor.rowcount == 1

    def register_pull_request(
        self,
        repository: str,
        pull_request_number: int,
        task_id: str,
        pull_request_id: str | None = None,
    ) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO github_service.pull_request_tasks "
                "(repository, pull_request_number, pull_request_id, task_id) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT(repository, pull_request_number) "
                "DO UPDATE SET pull_request_id = EXCLUDED.pull_request_id, "
                "task_id = EXCLUDED.task_id",
                (repository.lower(), pull_request_number, pull_request_id, task_id),
            )

    def task_for_pull_request(
        self,
        repository: str,
        pull_request_number: int,
    ) -> str | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT task_id FROM github_service.pull_request_tasks "
                "WHERE repository = %s AND pull_request_number = %s",
                (repository.lower(), pull_request_number),
            )
            row = cursor.fetchone()
            return None if row is None else str(row["task_id"])

    def pull_request_for_task(self, task_id: str) -> dict[str, object] | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT repository, pull_request_number, pull_request_id "
                "FROM github_service.pull_request_tasks WHERE task_id = %s",
                (task_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "repository": str(row["repository"]),
                "number": int(row["pull_request_number"]),
                "pull_request_id": row["pull_request_id"],
            }

    def task_context(self, task_id: str) -> dict[str, str] | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT project_node_id, project_item_id, target_repository "
                "FROM github_service.task_contexts WHERE task_id = %s",
                (task_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "project_node_id": str(row["project_node_id"]),
                "project_item_id": str(row["project_item_id"]),
                "target_repository": str(row["target_repository"]),
            }

    @contextmanager
    def publication_lock(self, task_id: str):
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (task_id,),
            )
            yield

    def close(self) -> None:
        return None
