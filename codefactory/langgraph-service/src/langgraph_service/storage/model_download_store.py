"""PostgreSQL queue and lease operations for inference model downloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any, Literal
from uuid import UUID, uuid4

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row

from langgraph_service.inference_config import DownloadStatus, InferenceInstance
from langgraph_service.model_download import (
    validate_download_url,
    validate_model_filename,
    validate_sha256,
)

FailureCode = Literal[
    "hash_mismatch", "too_large", "policy_rejected", "download_failed"
]
PUBLIC_FAILURES: dict[FailureCode, str] = {
    "hash_mismatch": "Model artifact SHA-256 did not match.",
    "too_large": "Model artifact exceeds the 100 GiB size limit.",
    "policy_rejected": "Model download was rejected by URL or network safety policy.",
    "download_failed": "Model download failed; verify artifact availability and retry.",
}
_TRANSITIONS = {
    "queued": frozenset({"downloading", "failed"}),
    "downloading": frozenset({"downloading", "verifying", "failed"}),
    "verifying": frozenset({"downloading", "ready", "failed"}),
    "ready": frozenset(),
    "failed": frozenset(),
}
_GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
ROLLOUT_FAILURE = "Model rollout failed; verify the inference deployment and retry."


def validate_download_transition(
    current: DownloadStatus, target: DownloadStatus
) -> None:
    if target not in _TRANSITIONS[current]:
        raise ValueError(
            f"download status cannot transition from {current} to {target}"
        )


@dataclass(frozen=True)
class ModelDownloadRequest:
    request_id: UUID
    model_filename: str
    model_url: str
    model_sha256: str
    requested_by: str
    status: DownloadStatus
    public_error: str | None
    attempts: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ClaimedModelDownload(ModelDownloadRequest):
    lease_token: UUID


@dataclass(frozen=True)
class ClaimedModelRollout:
    event_id: UUID
    instance_id: InferenceInstance
    rollout_request_id: UUID
    model_filename: str
    config_version: int
    idempotency_key: str
    lease_token: UUID
    lease_until: datetime
    attempts: int


class PostgresModelDownloadStore:
    def __init__(self, database_url: str, *, connection_factory: Any = psycopg.connect):
        self._database_url = database_url
        self._connection_factory = connection_factory

    def submit(
        self,
        *,
        model_filename: str,
        model_url: str,
        model_sha256: str,
        requested_by: str,
    ) -> ModelDownloadRequest:
        filename = validate_model_filename(model_filename)
        url = validate_download_url(model_url)
        digest = validate_sha256(model_sha256)
        if not isinstance(requested_by, str) or not _GITHUB_LOGIN.fullmatch(
            requested_by
        ):
            raise ValueError("requested_by must be a GitHub login")
        request_id = uuid4()
        try:
            with (
                self._connection_factory(
                    self._database_url, row_factory=dict_row
                ) as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute(
                    "SELECT request_id "
                    "FROM langgraph_service.inference_model_download_request "
                    "WHERE status IN ('queued', 'downloading', 'verifying') "
                    "FOR UPDATE"
                )
                if cursor.fetchone() is not None:
                    raise ValueError("a shared model download is already active")
                cursor.execute(
                    "INSERT INTO langgraph_service.inference_model_download_request "
                    "(request_id, instance_id, model_filename, model_url, model_sha256, "
                    "requested_by, config_version) "
                    "VALUES (%s, NULL, %s, %s, %s, %s, 0) "
                    "RETURNING request_id, model_filename, model_url, model_sha256, "
                    "requested_by, status, public_error, attempts, created_at, updated_at",
                    (request_id, filename, url, digest, requested_by),
                )
                row = cursor.fetchone()
        except UniqueViolation as error:
            raise ValueError("a shared model download is already active") from error
        return self._request(row)

    def get(self, request_id: UUID) -> ModelDownloadRequest | None:
        with (
            self._connection_factory(
                self._database_url, row_factory=dict_row
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT request_id, model_filename, model_url, model_sha256, "
                "requested_by, status, public_error, attempts, created_at, updated_at "
                "FROM langgraph_service.inference_model_download_request "
                "WHERE request_id = %s",
                (request_id,),
            )
            row = cursor.fetchone()
        return self._request(row) if row is not None else None

    def claim(self, *, lease_seconds: int = 120) -> ClaimedModelDownload | None:
        if lease_seconds < 30:
            raise ValueError("model download lease must be at least 30 seconds")
        lease_token = uuid4()
        with (
            self._connection_factory(
                self._database_url, row_factory=dict_row
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "WITH candidate AS ("
                " SELECT request_id FROM langgraph_service.inference_model_download_request"
                " WHERE status = 'queued' OR (status IN ('downloading', 'verifying')"
                " AND lease_until < now())"
                " ORDER BY created_at, request_id LIMIT 1 FOR UPDATE SKIP LOCKED"
                ") UPDATE langgraph_service.inference_model_download_request AS request"
                " SET status = 'downloading', attempts = request.attempts + 1,"
                " lease_token = %s, lease_until = now() + (%s * interval '1 second'),"
                " public_error = NULL, updated_at = now()"
                " FROM candidate WHERE request.request_id = candidate.request_id"
                " RETURNING request.request_id, request.model_filename,"
                " request.model_url, request.model_sha256, request.requested_by, request.status,"
                " request.public_error, request.attempts, request.created_at, request.updated_at,"
                " request.lease_token",
                (lease_token, lease_seconds),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return ClaimedModelDownload(
            **self._request_values(row), lease_token=row["lease_token"]
        )

    def renew(
        self, request_id: UUID, lease_token: UUID, *, lease_seconds: int = 120
    ) -> bool:
        with (
            self._connection_factory(
                self._database_url, row_factory=dict_row
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE langgraph_service.inference_model_download_request "
                "SET lease_until = now() + (%s * interval '1 second'), updated_at = now() "
                "WHERE request_id = %s AND lease_token = %s "
                "AND status IN ('downloading', 'verifying') AND lease_until > now() "
                "RETURNING request_id",
                (lease_seconds, request_id, lease_token),
            )
            return cursor.fetchone() is not None

    def mark_verifying(self, request_id: UUID, lease_token: UUID) -> None:
        self._transition_request(
            request_id,
            lease_token,
            expected="downloading",
            target="verifying",
            public_error=None,
        )

    def complete(self, request_id: UUID, lease_token: UUID) -> None:
        with (
            self._connection_factory(
                self._database_url, row_factory=dict_row
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE langgraph_service.inference_model_download_request "
                "SET status = 'ready', public_error = NULL, lease_token = NULL, "
                "lease_until = NULL, updated_at = now() "
                "WHERE request_id = %s AND lease_token = %s AND lease_until > now() "
                "AND status = 'verifying' "
                "RETURNING request_id",
                (request_id, lease_token),
            )
            request = cursor.fetchone()
            if request is None:
                raise ValueError("download lease is no longer active")

    def claim_rollout(self, *, lease_seconds: int = 120) -> ClaimedModelRollout | None:
        if not 30 <= lease_seconds <= 900:
            raise ValueError("model rollout lease must be between 30 and 900 seconds")
        lease_token = uuid4()
        with (
            self._connection_factory(
                self._database_url, row_factory=dict_row
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE langgraph_service.inference_model_rollout_outbox AS event "
                "SET status = 'superseded', lease_token = NULL, lease_until = NULL, "
                "updated_at = now() "
                "WHERE event.status IN ('pending', 'leased') AND NOT EXISTS ("
                "SELECT 1 FROM langgraph_service.inference_instance_config AS config "
                "WHERE config.instance_id = event.instance_id "
                "AND config.rollout_request_id = event.rollout_request_id "
                "AND config.model_filename = event.model_filename "
                "AND config.version = event.config_version)"
            )
            cursor.execute(
                "SELECT event.event_id, event.instance_id, event.rollout_request_id, "
                "event.model_filename, event.config_version, event.idempotency_key "
                "FROM langgraph_service.inference_model_rollout_outbox AS event "
                "JOIN langgraph_service.inference_instance_config AS config "
                "ON config.instance_id = event.instance_id "
                "AND config.rollout_request_id = event.rollout_request_id "
                "AND config.model_filename = event.model_filename "
                "AND config.version = event.config_version "
                "AND config.rollout_status IN ('pending', 'rolling_out') "
                "WHERE event.status = 'pending' OR (event.status = 'leased' "
                "AND event.lease_until < now()) "
                "ORDER BY event.created_at, event.event_id "
                "LIMIT 1 FOR UPDATE OF event SKIP LOCKED"
            )
            candidate = cursor.fetchone()
            if candidate is None:
                return None
            cursor.execute(
                "UPDATE langgraph_service.inference_model_rollout_outbox "
                "SET status = 'leased', lease_token = %s, "
                "lease_until = now() + (%s * interval '1 second'), "
                "attempts = attempts + 1, updated_at = now() "
                "WHERE event_id = %s "
                "RETURNING event_id, instance_id, rollout_request_id, model_filename, "
                "config_version, idempotency_key, lease_token, lease_until, attempts",
                (lease_token, lease_seconds, candidate["event_id"]),
            )
            row = cursor.fetchone()
            cursor.execute(
                "UPDATE langgraph_service.inference_instance_config "
                "SET rollout_status = 'rolling_out' "
                "WHERE instance_id = %s AND rollout_request_id = %s "
                "AND version = %s AND model_filename = %s "
                "AND rollout_status IN ('pending', 'rolling_out') "
                "RETURNING instance_id",
                (
                    row["instance_id"],
                    row["rollout_request_id"],
                    row["config_version"],
                    row["model_filename"],
                ),
            )
            if cursor.fetchone() is None:
                cursor.execute(
                    "UPDATE langgraph_service.inference_model_rollout_outbox "
                    "SET status = 'superseded', lease_token = NULL, lease_until = NULL, "
                    "updated_at = now() WHERE event_id = %s",
                    (row["event_id"],),
                )
                return None
        return ClaimedModelRollout(**row)

    def acknowledge_rollout(
        self,
        event_id: UUID,
        lease_token: UUID,
        *,
        outcome: Literal["succeeded", "failed"],
    ) -> str:
        if outcome not in {"succeeded", "failed"}:
            raise ValueError("invalid rollout outcome")
        with (
            self._connection_factory(
                self._database_url, row_factory=dict_row
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT event_id, rollout_request_id, instance_id, model_filename, "
                "config_version, status, lease_token, lease_until, "
                "(lease_until > now()) AS lease_active "
                "FROM langgraph_service.inference_model_rollout_outbox "
                "WHERE event_id = %s FOR UPDATE",
                (event_id,),
            )
            event = cursor.fetchone()
            if event is None:
                raise ValueError("rollout event not found")
            if event["status"] in {"succeeded", "failed"}:
                if event["status"] == outcome and event["lease_token"] == lease_token:
                    return event["status"]
                raise ValueError("rollout acknowledgment is no longer active")
            if event["status"] != "leased" or event["lease_token"] != lease_token:
                raise ValueError("rollout acknowledgment is no longer active")
            if not event["lease_active"]:
                raise ValueError("rollout acknowledgment is no longer active")
            cursor.execute(
                "SELECT instance_id FROM langgraph_service.inference_instance_config "
                "WHERE instance_id = %s AND rollout_request_id = %s "
                "AND model_filename = %s AND version = %s "
                "AND rollout_status = 'rolling_out' "
                "FOR UPDATE",
                (
                    event["instance_id"],
                    event["rollout_request_id"],
                    event["model_filename"],
                    event["config_version"],
                ),
            )
            if cursor.fetchone() is None:
                cursor.execute(
                    "UPDATE langgraph_service.inference_model_rollout_outbox "
                    "SET status = 'superseded', lease_token = NULL, lease_until = NULL, "
                    "updated_at = now() WHERE event_id = %s",
                    (event_id,),
                )
                return "superseded"
            cursor.execute(
                "UPDATE langgraph_service.inference_instance_config "
                "SET rollout_status = %s, rollout_error = %s "
                "WHERE instance_id = %s AND rollout_request_id = %s "
                "AND version = %s AND rollout_status = 'rolling_out'",
                (
                    "ready" if outcome == "succeeded" else "failed",
                    None if outcome == "succeeded" else ROLLOUT_FAILURE,
                    event["instance_id"],
                    event["rollout_request_id"],
                    event["config_version"],
                ),
            )
            cursor.execute(
                "UPDATE langgraph_service.inference_model_rollout_outbox "
                "SET status = %s, lease_until = NULL, updated_at = now() "
                "WHERE event_id = %s",
                (outcome, event_id),
            )
        return outcome

    def fail(self, request_id: UUID, lease_token: UUID, *, code: FailureCode) -> None:
        self._transition_request(
            request_id,
            lease_token,
            expected=None,
            target="failed",
            public_error=PUBLIC_FAILURES[code],
        )

    def _transition_request(
        self,
        request_id: UUID,
        lease_token: UUID,
        *,
        expected: DownloadStatus | None,
        target: DownloadStatus,
        public_error: str | None,
    ) -> None:
        if expected is not None:
            validate_download_transition(expected, target)
        elif target != "failed":
            raise ValueError(
                "only an active download may transition without an exact state"
            )
        with (
            self._connection_factory(
                self._database_url, row_factory=dict_row
            ) as connection,
            connection.cursor() as cursor,
        ):
            expected_sql = (
                "status = %s"
                if expected is not None
                else "status IN ('downloading', 'verifying')"
            )
            lease_sql = (
                "lease_token = NULL, lease_until = NULL, "
                if target in {"ready", "failed"}
                else ""
            )
            parameters: tuple[object, ...] = (
                target,
                public_error,
                request_id,
                lease_token,
            )
            if expected is not None:
                parameters += (expected,)
            cursor.execute(
                "UPDATE langgraph_service.inference_model_download_request "
                "SET status = %s, public_error = %s, "
                f"{lease_sql}updated_at = now() "
                f"WHERE request_id = %s AND lease_token = %s AND lease_until > now() "
                f"AND {expected_sql} "
                "RETURNING request_id",
                parameters,
            )
            if cursor.fetchone() is None:
                raise ValueError("download lease is no longer active")

    @staticmethod
    def _request_values(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "request_id": row["request_id"],
            "model_filename": row["model_filename"],
            "model_url": row["model_url"],
            "model_sha256": row["model_sha256"],
            "requested_by": row["requested_by"],
            "status": row["status"],
            "public_error": row["public_error"],
            "attempts": row["attempts"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @classmethod
    def _request(cls, row: dict[str, Any]) -> ModelDownloadRequest:
        return ModelDownloadRequest(**cls._request_values(row))
