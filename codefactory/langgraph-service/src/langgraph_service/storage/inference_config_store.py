"""Durable, audited GPU and CPU inference model selections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any

import psycopg
from psycopg.rows import dict_row

from langgraph_service.inference_config import (
    InferenceInstance,
    InferenceInstanceConfig,
)

_GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")


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
                "SELECT instance_id, model_filename, model_url, model_sha256, "
                "download_status, current_request_id, download_error, version, "
                "rollout_status, rollout_request_id, rollout_error, updated_at, updated_by "
                "FROM langgraph_service.inference_instance_config "
                "ORDER BY instance_id"
            )
            rows = cursor.fetchall()
        return {row["instance_id"]: self._snapshot(row) for row in rows}

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
        with (
            self._connection_factory(
                self._database_url, row_factory=dict_row
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE langgraph_service.inference_instance_config "
                "SET model_filename = %s, version = version + 1, "
                "rollout_status = 'not_requested', rollout_request_id = NULL, "
                "rollout_error = NULL, updated_at = now(), updated_by = %s "
                "WHERE instance_id = %s "
                "AND download_status NOT IN ('queued', 'downloading', 'verifying') "
                "AND rollout_status NOT IN ('pending', 'rolling_out') "
                "RETURNING instance_id, model_filename, model_url, model_sha256, "
                "download_status, current_request_id, download_error, version, "
                "rollout_status, rollout_request_id, rollout_error, updated_at, updated_by",
                (config.model_filename, updated_by, config.instance_id),
            )
            row = cursor.fetchone()
        if row is None:
            with (
                self._connection_factory(
                    self._database_url, row_factory=dict_row
                ) as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute(
                    "SELECT instance_id, rollout_status "
                    "FROM langgraph_service.inference_instance_config "
                    "WHERE instance_id = %s",
                    (config.instance_id,),
                )
                previous = cursor.fetchone()
            if previous is None:
                raise ValueError("unknown inference instance")
            if previous.get("rollout_status") in {"pending", "rolling_out"}:
                raise ValueError("inference model rollout is active")
            raise ValueError("inference model download is active")
        return self._snapshot(row)

    @staticmethod
    def _snapshot(row: dict[str, Any]) -> InferenceInstanceSnapshot:
        return InferenceInstanceSnapshot(
            config=InferenceInstanceConfig(
                instance_id=row["instance_id"],
                model_filename=row["model_filename"],
                model_url=row.get("model_url"),
                model_sha256=row.get("model_sha256"),
                download_status=row.get("download_status", "ready"),
                download_request_id=(
                    str(row["current_request_id"])
                    if row.get("current_request_id") is not None
                    else None
                ),
                download_error=row.get("download_error"),
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
