"""Durable, audited GPU and CPU inference model selections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from pydantic import TypeAdapter

from langgraph_service.inference_config import (
    InferenceInstance,
    InferenceInstanceConfig,
)

_GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
_INSTANCE_ID_ADAPTER = TypeAdapter(InferenceInstance)


@dataclass(frozen=True)
class InferenceInstanceSnapshot:
    config: InferenceInstanceConfig
    version: int
    updated_at: datetime | None
    updated_by: str | None


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
                "SELECT instance_id, model_filename, version, rollout_status, "
                "rollout_request_id, rollout_error, updated_at, updated_by "
                "FROM langgraph_service.inference_instance_config "
                "WHERE is_active "
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
                "SELECT instance_id FROM langgraph_service.inference_instance_config "
                "WHERE instance_id = %s",
                (config.instance_id,),
            )
            if cursor.fetchone() is not None:
                raise ValueError("inference instance already exists")
            cursor.execute(
                "INSERT INTO langgraph_service.inference_instance_config "
                "(instance_id, model_filename, version, updated_at, updated_by, "
                "is_active, rollout_status) "
                "VALUES (%s, %s, 1, now(), %s, TRUE, 'not_requested') "
                "RETURNING instance_id, model_filename, version, rollout_status, "
                "rollout_request_id, rollout_error, updated_at, updated_by",
                (config.instance_id, config.model_filename, created_by),
            )
            row = cursor.fetchone()
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
                "SELECT instance_id, download_status, rollout_status "
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
            cursor.execute(
                "SELECT count(*) AS active_count "
                "FROM langgraph_service.inference_instance_config WHERE is_active"
            )
            if cursor.fetchone()["active_count"] <= 1:
                raise ValueError("at least one inference instance must remain")
            cursor.execute(
                "UPDATE langgraph_service.inference_instance_config "
                "SET is_active = FALSE WHERE instance_id = %s AND is_active",
                (instance_id,),
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
                "SELECT version, rollout_status "
                "FROM langgraph_service.inference_instance_config "
                "WHERE instance_id = %s AND is_active FOR UPDATE",
                (config.instance_id,),
            )
            previous = cursor.fetchone()
            if previous is None:
                raise ValueError("unknown inference instance")
            if previous["rollout_status"] in {"pending", "rolling_out"}:
                raise ValueError("inference model rollout is active")
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

    @staticmethod
    def _snapshot(row: dict[str, Any]) -> InferenceInstanceSnapshot:
        return InferenceInstanceSnapshot(
            config=InferenceInstanceConfig(
                instance_id=row["instance_id"],
                model_filename=row["model_filename"],
                rollout_status=row.get("rollout_status", "not_requested"),
                rollout_request_id=(
                    str(row["rollout_request_id"])
                    if row.get("rollout_request_id") is not None
                    else None
                ),
                rollout_error=row.get("rollout_error"),
            ),
            version=row["version"],
            updated_at=row["updated_at"],
            updated_by=row["updated_by"],
        )
