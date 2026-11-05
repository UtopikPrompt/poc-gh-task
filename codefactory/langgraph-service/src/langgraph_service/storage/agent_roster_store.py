"""PostgreSQL persistence for active agent rosters and task-start snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re
from collections.abc import Mapping
from typing import Any

import psycopg
from psycopg.rows import dict_row

from langgraph_service.agent_roster import AgentProfile, AgentRoster, ROLE_TOOL_PROFILES

_GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")


@dataclass(frozen=True)
class AgentRosterSnapshot:
    roster: AgentRoster | None
    revision: int
    updated_at: datetime | None
    updated_by: str | None


class PostgresAgentRosterStore:
    def __init__(self, database_url: str, *, connection_factory: Any = psycopg.connect):
        self._database_url = database_url
        self._connection_factory = connection_factory

    def get(self) -> AgentRosterSnapshot:
        with (
            self._connection_factory(
                self._database_url, row_factory=dict_row
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT roster_document, revision, updated_at, updated_by "
                "FROM langgraph_service.agent_roster WHERE id = 1"
            )
            row = cursor.fetchone()
        return _roster_snapshot(row)

    def update(
        self,
        roster: AgentRoster,
        *,
        expected_revision: int,
        updated_by: str,
    ) -> AgentRosterSnapshot:
        if not isinstance(roster, AgentRoster):
            roster = AgentRoster.model_validate(roster)
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ValueError("expected_revision must be a non-negative integer")
        if not isinstance(updated_by, str) or not _GITHUB_LOGIN.fullmatch(updated_by):
            raise ValueError("updated_by must be a GitHub login")
        document = json.dumps(roster.model_dump(mode="json"), separators=(",", ":"))
        with (
            self._connection_factory(
                self._database_url, row_factory=dict_row
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE langgraph_service.agent_roster "
                "SET roster_document = %s::jsonb, revision = revision + 1, "
                "updated_at = now(), updated_by = %s "
                "WHERE id = 1 AND revision = %s "
                "RETURNING roster_document, revision, updated_at, updated_by",
                (document, updated_by, expected_revision),
            )
            row = cursor.fetchone()
        if row is None:
            raise ValueError("active roster revision changed")
        return _roster_snapshot(row)

    def resolve_for_task(self, task_id: str) -> dict[str, object]:
        with (
            self._connection_factory(
                self._database_url, row_factory=dict_row
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT roster_revision, profile_snapshot "
                "FROM langgraph_service.task_agent_roster_snapshot "
                "WHERE task_id = %s",
                (task_id,),
            )
            row = cursor.fetchone()
            if row is not None:
                return _task_snapshot(row)

            cursor.execute(
                "SELECT roster_document, revision "
                "FROM langgraph_service.agent_roster WHERE id = 1 FOR SHARE"
            )
            active = cursor.fetchone()
            snapshot = _roster_snapshot(active)
            if snapshot.roster is None:
                raise ValueError("an active agent roster must be configured first")
            profile_snapshot = snapshot.roster.resolved_profiles()
            encoded = json.dumps(profile_snapshot, separators=(",", ":"))
            cursor.execute(
                "INSERT INTO langgraph_service.task_agent_roster_snapshot "
                "(task_id, roster_revision, profile_snapshot) "
                "VALUES (%s, %s, %s::jsonb) ON CONFLICT (task_id) DO NOTHING",
                (task_id, snapshot.revision, encoded),
            )
            cursor.execute(
                "SELECT roster_revision, profile_snapshot "
                "FROM langgraph_service.task_agent_roster_snapshot "
                "WHERE task_id = %s",
                (task_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise RuntimeError("task roster snapshot was not persisted")
        return _task_snapshot(row)


def _roster_snapshot(row: object) -> AgentRosterSnapshot:
    if not isinstance(row, dict):
        return AgentRosterSnapshot(None, 0, None, None)
    raw_roster = row.get("roster_document")
    if isinstance(raw_roster, str):
        raw_roster = json.loads(raw_roster)
    roster = AgentRoster.model_validate(raw_roster) if raw_roster is not None else None
    return AgentRosterSnapshot(
        roster=roster,
        revision=row["revision"],
        updated_at=row.get("updated_at"),
        updated_by=row.get("updated_by"),
    )


def _task_snapshot(row: dict[str, object]) -> dict[str, object]:
    raw_profiles = row["profile_snapshot"]
    if isinstance(raw_profiles, str):
        raw_profiles = json.loads(raw_profiles)
    if (
        not isinstance(raw_profiles, Mapping)
        or set(raw_profiles) != set(ROLE_TOOL_PROFILES)
        or not isinstance(row["roster_revision"], int)
        or isinstance(row["roster_revision"], bool)
        or row["roster_revision"] < 1
    ):
        raise ValueError("persisted task roster snapshot is invalid")
    profiles = {}
    for role, raw_profile in raw_profiles.items():
        profile = AgentProfile.model_validate(raw_profile)
        if profile.tool_profile != ROLE_TOOL_PROFILES[role]:
            raise ValueError(
                "persisted task roster snapshot has an invalid tool profile"
            )
        profiles[role] = profile.model_dump(mode="json")
    return {"revision": row["roster_revision"], "profiles": profiles}
