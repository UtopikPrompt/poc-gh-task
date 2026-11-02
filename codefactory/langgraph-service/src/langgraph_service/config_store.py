"""Durable, audited agent configuration overrides."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any

import psycopg
from psycopg.rows import dict_row

from langgraph_service.agent_config import AgentConfig

_GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")


@dataclass(frozen=True)
class AgentConfigSnapshot:
    config: AgentConfig
    version: int
    updated_at: datetime | None
    updated_by: str | None


class PostgresAgentConfigStore:
    def __init__(self, database_url: str, *, connection_factory: Any = psycopg.connect):
        self._database_url = database_url
        self._connection_factory = connection_factory

    def get(self) -> AgentConfigSnapshot:
        with (
            self._connection_factory(
                self._database_url, row_factory=dict_row
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT completion_timeout_seconds, version, updated_at, updated_by "
                "FROM langgraph_service.agent_config WHERE id = 1"
            )
            row = cursor.fetchone()
        if row is None:
            return AgentConfigSnapshot(AgentConfig(), 0, None, None)
        return AgentConfigSnapshot(
            config=AgentConfig(
                completion_timeout_seconds=row["completion_timeout_seconds"]
            ),
            version=row["version"],
            updated_at=row["updated_at"],
            updated_by=row["updated_by"],
        )

    def update(self, config: AgentConfig, *, updated_by: str) -> AgentConfigSnapshot:
        if not isinstance(config, AgentConfig):
            config = AgentConfig.model_validate(config)
        if not isinstance(updated_by, str) or not _GITHUB_LOGIN.fullmatch(updated_by):
            raise ValueError("updated_by must be a GitHub login")
        with (
            self._connection_factory(
                self._database_url, row_factory=dict_row
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "INSERT INTO langgraph_service.agent_config "
                "(id, completion_timeout_seconds, version, updated_at, updated_by) "
                "VALUES (1, %s, 1, now(), %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "completion_timeout_seconds = EXCLUDED.completion_timeout_seconds, "
                "version = langgraph_service.agent_config.version + 1, "
                "updated_at = now(), updated_by = EXCLUDED.updated_by "
                "RETURNING completion_timeout_seconds, version, updated_at, updated_by",
                (config.completion_timeout_seconds, updated_by),
            )
            row = cursor.fetchone()
        return AgentConfigSnapshot(
            config=AgentConfig(
                completion_timeout_seconds=row["completion_timeout_seconds"]
            ),
            version=row["version"],
            updated_at=row["updated_at"],
            updated_by=row["updated_by"],
        )
