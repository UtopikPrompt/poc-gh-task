"""Durable, audited GPU and CPU inference model selections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from pydantic import TypeAdapter

from langgraph_service.inference_config import (
    InferenceInstance,
    InferenceInstanceConfig,
)

_GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
_INSTANCE_ID_ADAPTER = TypeAdapter(InferenceInstance)
WORKLOAD_FAILURE = "Inference workload reconciliation failed; check the inference controller and retry."


@dataclass(frozen=True)
class InferenceInstanceSnapshot:
    config: InferenceInstanceConfig
    version: int
    updated_at: datetime | None
    updated_by: str | None


@dataclass(frozen=True)
class ClaimedInferenceWorkload:
    event_id: UUID
    instance_id: InferenceInstance
    operation: str
    model_filename: str
    context_size: int
    threads: int
    lease_token: UUID
    attempts: int


class PostgresInferenceInstanceConfigStore:
    def __init__(self, database_url: str, *, connection_factory: Any = psycopg.connect):
        self._database_url = database_url
        self._connection_factory = connection_factory

    def get_all(self) -> dict[InferenceInstance, InferenceInstanceSnapshot]:
        with (
            self._connection_factory(
                self._database_url, row_factory=dict_row
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT instance_id, model_filename, context_size, threads, version, rollout_status, "
                "rollout_request_id, rollout_error, workload_status, workload_error, "
                "updated_at, updated_by "
                "FROM langgraph_service.inference_instance_config "
                "WHERE is_active OR workload_status IN ('deleting', 'failed') "
                "ORDER BY instance_id"
            )
            rows = cursor.fetchall()
        return {row["instance_id"]: self._snapshot(row) for row in rows}

    def create(
        self,
        config: InferenceInstanceConfig,
        *,
        created_by: str,
    ) -> InferenceInstanceSnapshot:
        if not isinstance(config, InferenceInstanceConfig):
            config = InferenceInstanceConfig.model_validate(config)
        if not isinstance(created_by, str) or not _GITHUB_LOGIN.fullmatch(created_by):
            raise ValueError("created_by must be a GitHub login")
        with (
            self._connection_factory(
                self._database_url, row_factory=dict_row
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "LOCK TABLE langgraph_service.inference_instance_config "
                "IN SHARE ROW EXCLUSIVE MODE"
            )
            cursor.execute(
                "SELECT is_active, workload_status "
                "FROM langgraph_service.inference_instance_config "
                "WHERE instance_id = %s FOR UPDATE",
                (config.instance_id,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if existing["is_active"] or existing["workload_status"] != "deleted":
                    raise ValueError("inference instance already exists")
                cursor.execute(
                    "UPDATE langgraph_service.inference_instance_config "
                    "SET model_filename = %s, context_size = %s, threads = %s, "
                    "version = version + 1, updated_at = now(), updated_by = %s, "
                    "is_active = TRUE, rollout_status = 'not_requested', "
                    "rollout_request_id = NULL, rollout_error = NULL, "
                    "workload_status = 'provisioning', workload_error = NULL, "
                    "current_request_id = NULL, download_status = 'ready', "
                    "download_error = NULL, model_url = NULL, model_sha256 = NULL "
                    "WHERE instance_id = %s AND NOT is_active "
                    "AND workload_status = 'deleted' "
                    "RETURNING instance_id, model_filename, context_size, threads, "
                    "version, rollout_status, rollout_request_id, rollout_error, "
                    "workload_status, workload_error, updated_at, updated_by",
                    (
                        config.model_filename,
                        config.context_size,
                        config.threads,
                        created_by,
                        config.instance_id,
                    ),
                )
            else:
                cursor.execute(
                    "INSERT INTO langgraph_service.inference_instance_config "
                    "(instance_id, model_filename, context_size, threads, version, updated_at, updated_by, "
                    "is_active, rollout_status, workload_status) "
                    "VALUES (%s, %s, %s, %s, 1, now(), %s, TRUE, 'not_requested', 'provisioning') "
                    "RETURNING instance_id, model_filename, context_size, threads, version, rollout_status, "
                    "rollout_request_id, rollout_error, workload_status, workload_error, "
                    "updated_at, updated_by",
                    (
                        config.instance_id,
                        config.model_filename,
                        config.context_size,
                        config.threads,
                        created_by,
                    ),
                )
            row = cursor.fetchone()
            cursor.execute(
                "INSERT INTO langgraph_service.inference_workload_outbox "
                "(event_id, instance_id, operation) VALUES (%s, %s, 'ensure')",
                (uuid4(), config.instance_id),
            )
        return self._snapshot(row)

    def delete(self, instance_id: InferenceInstance) -> None:
        instance_id = _INSTANCE_ID_ADAPTER.validate_python(instance_id)
        with (
            self._connection_factory(
                self._database_url, row_factory=dict_row
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "LOCK TABLE langgraph_service.inference_instance_config "
                "IN SHARE ROW EXCLUSIVE MODE"
            )
            cursor.execute(
                "SELECT instance_id, download_status, rollout_status, workload_status "
                "FROM langgraph_service.inference_instance_config "
                "WHERE instance_id = %s AND is_active FOR UPDATE",
                (instance_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("unknown inference instance")
            if row["download_status"] in {"queued", "downloading", "verifying"}:
                raise ValueError("inference model download is active")
            if row["rollout_status"] in {"pending", "rolling_out"}:
                raise ValueError("inference model rollout is active")
            if row["workload_status"] != "ready":
                raise ValueError("inference workload is not ready")
            cursor.execute(
                "UPDATE langgraph_service.inference_instance_config "
                "SET is_active = FALSE, workload_status = 'deleting', "
                "workload_error = NULL WHERE instance_id = %s AND is_active",
                (instance_id,),
            )
            cursor.execute(
                "INSERT INTO langgraph_service.inference_workload_outbox "
                "(event_id, instance_id, operation) VALUES (%s, %s, 'delete')",
                (uuid4(), instance_id),
            )

    def update(
        self,
        config: InferenceInstanceConfig,
        *,
        updated_by: str,
    ) -> InferenceInstanceSnapshot:
        if not isinstance(config, InferenceInstanceConfig):
            config = InferenceInstanceConfig.model_validate(config)
        if not isinstance(updated_by, str) or not _GITHUB_LOGIN.fullmatch(updated_by):
            raise ValueError("updated_by must be a GitHub login")
        rollout_request_id = uuid4()
        with (
            self._connection_factory(
                self._database_url, row_factory=dict_row
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT version, rollout_status, workload_status "
                "FROM langgraph_service.inference_instance_config "
                "WHERE instance_id = %s AND is_active FOR UPDATE",
                (config.instance_id,),
            )
            previous = cursor.fetchone()
            if previous is None:
                raise ValueError("unknown inference instance")
            if previous["rollout_status"] in {"pending", "rolling_out"}:
                raise ValueError("inference model rollout is active")
            if previous["workload_status"] != "ready":
                raise ValueError("inference workload is not ready")
            version = previous["version"] + 1
            cursor.execute(
                "INSERT INTO langgraph_service.inference_model_rollout_outbox "
                "(event_id, rollout_request_id, instance_id, model_filename, "
                "config_version, idempotency_key) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    rollout_request_id,
                    rollout_request_id,
                    config.instance_id,
                    config.model_filename,
                    version,
                    f"{config.instance_id}:{version}:{rollout_request_id}",
                ),
            )
            cursor.execute(
                "UPDATE langgraph_service.inference_instance_config "
                "SET model_filename = %s, version = %s, "
                "rollout_status = 'pending', rollout_request_id = %s, "
                "rollout_error = NULL, updated_at = now(), updated_by = %s "
                "WHERE instance_id = %s "
                "AND rollout_status NOT IN ('pending', 'rolling_out') "
                "AND is_active "
                "RETURNING instance_id, model_filename, version, "
                "context_size, threads, "
                "rollout_status, rollout_request_id, rollout_error, updated_at, updated_by",
                (
                    config.model_filename,
                    version,
                    rollout_request_id,
                    updated_by,
                    config.instance_id,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("inference model rollout is active")
        return self._snapshot(row)

    def claim_workload(self, lease_seconds: int) -> ClaimedInferenceWorkload | None:
        if not 30 <= lease_seconds <= 900:
            raise ValueError("lease_seconds must be between 30 and 900")
        lease_token = uuid4()
        with (
            self._connection_factory(
                self._database_url, row_factory=dict_row
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT event.event_id, event.instance_id, event.operation, "
                "event.attempts, config.model_filename, config.context_size, config.threads "
                "FROM langgraph_service.inference_workload_outbox AS event "
                "JOIN langgraph_service.inference_instance_config AS config "
                "ON config.instance_id = event.instance_id "
                "WHERE (event.status = 'pending' AND event.available_at <= now()) OR "
                "(event.status = 'leased' AND event.lease_until <= now()) "
                "ORDER BY event.created_at, event.event_id "
                "FOR UPDATE OF event SKIP LOCKED LIMIT 1"
            )
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute(
                "UPDATE langgraph_service.inference_workload_outbox "
                "SET status = 'leased', lease_token = %s, "
                "lease_until = now() + (%s * interval '1 second'), "
                "attempts = attempts + 1, updated_at = now() "
                "WHERE event_id = %s",
                (lease_token, lease_seconds, row["event_id"]),
            )
            cursor.execute(
                "UPDATE langgraph_service.inference_instance_config "
                "SET workload_status = %s, workload_error = NULL "
                "WHERE instance_id = %s",
                (
                    "provisioning" if row["operation"] == "ensure" else "deleting",
                    row["instance_id"],
                ),
            )
        return ClaimedInferenceWorkload(
            event_id=row["event_id"],
            instance_id=row["instance_id"],
            operation=row["operation"],
            model_filename=row["model_filename"],
            context_size=row["context_size"],
            threads=row["threads"],
            lease_token=lease_token,
            attempts=row["attempts"] + 1,
        )

    def acknowledge_workload(
        self, event_id: UUID, lease_token: UUID, *, succeeded: bool
    ) -> str:
        with (
            self._connection_factory(
                self._database_url, row_factory=dict_row
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT instance_id, operation, status, lease_token, "
                "completed_lease_token, (lease_until > now()) AS lease_active "
                "FROM langgraph_service.inference_workload_outbox "
                "WHERE event_id = %s FOR UPDATE",
                (event_id,),
            )
            event = cursor.fetchone()
            if event is None:
                raise ValueError("unknown inference workload event")
            if event["status"] == "succeeded":
                if event["completed_lease_token"] == lease_token:
                    return "succeeded"
                raise ValueError("inference workload lease is stale")
            if (
                event["status"] != "leased"
                or event["lease_token"] != lease_token
                or not event["lease_active"]
            ):
                raise ValueError("inference workload lease is stale")
            terminal_status = "succeeded" if succeeded else "pending"
            cursor.execute(
                "UPDATE langgraph_service.inference_workload_outbox "
                "SET status = %s, lease_token = NULL, lease_until = NULL, "
                "completed_lease_token = %s, "
                "available_at = CASE WHEN %s = 'pending' "
                "THEN now() + interval '30 seconds' ELSE available_at END, "
                "updated_at = now() "
                "WHERE event_id = %s",
                (
                    terminal_status,
                    lease_token if succeeded else None,
                    terminal_status,
                    event_id,
                ),
            )
            workload_status = (
                ("ready" if event["operation"] == "ensure" else "deleted")
                if succeeded
                else "failed"
            )
            cursor.execute(
                "UPDATE langgraph_service.inference_instance_config "
                "SET workload_status = %s, workload_error = %s "
                "WHERE instance_id = %s",
                (
                    workload_status,
                    None if succeeded else WORKLOAD_FAILURE,
                    event["instance_id"],
                ),
            )
        return "succeeded" if succeeded else "retrying"

    @staticmethod
    def _snapshot(row: dict[str, Any]) -> InferenceInstanceSnapshot:
        return InferenceInstanceSnapshot(
            config=InferenceInstanceConfig(
                instance_id=row["instance_id"],
                model_filename=row["model_filename"],
                context_size=row.get("context_size", 1024),
                threads=row.get("threads", 2),
                rollout_status=row.get("rollout_status", "not_requested"),
                rollout_request_id=(
                    str(row["rollout_request_id"])
                    if row.get("rollout_request_id") is not None
                    else None
                ),
                rollout_error=row.get("rollout_error"),
                workload_status=row.get("workload_status", "ready"),
                workload_error=row.get("workload_error"),
            ),
            version=row["version"],
            updated_at=row["updated_at"],
            updated_by=row["updated_by"],
        )
