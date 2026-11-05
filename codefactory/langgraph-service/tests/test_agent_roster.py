import json

import pytest
from pydantic import ValidationError

from langgraph_service.agent_roster import AgentRoster
from langgraph_service.storage.agent_roster_store import PostgresAgentRosterStore

ROLE_TO_TOOL = {
    "context": "gather_context",
    "planning": "create_plan",
    "risk": "assess_risk",
    "implementation": "prepare_changes",
    "repair": "repair_changes",
}


def _roster(prompt_suffix="v1"):
    agents = [
        {
            "agent_id": role,
            "name": f"{role} specialist",
            "description": f"Handles the {role} stage.",
            "prompt_content": f"Follow the {prompt_suffix} profile.",
            "model_profile": "local_llama_cpp",
            "skill_ids": ["bounded_review"],
            "tool_profile": tool,
        }
        for role, tool in ROLE_TO_TOOL.items()
    ]
    return AgentRoster.model_validate(
        {"agents": agents, "assignments": {role: role for role in ROLE_TO_TOOL}}
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("model_profile", "unregistered_model"), ("tool_profile", "shell")],
)
def test_agent_roster_rejects_unsupported_model_and_tool_profiles(field, value):
    roster = _roster().model_dump(mode="json")
    roster["agents"][0][field] = value

    with pytest.raises(ValidationError):
        AgentRoster.model_validate(roster)


def test_agent_roster_resolves_only_assigned_profiles():
    roster = _roster()

    resolved = roster.resolved_profiles()

    assert set(resolved) == set(ROLE_TO_TOOL)
    assert resolved["context"]["prompt_content"] == "Follow the v1 profile."
    assert resolved["context"]["skill_ids"] == ["bounded_review"]


def test_postgres_roster_store_pins_task_snapshot_across_active_revision_changes():
    state = {
        "active": {
            "roster_document": None,
            "revision": 0,
            "updated_at": None,
            "updated_by": None,
        },
        "tasks": {},
        "active_reads": 0,
    }

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, query, parameters=()):
            if "FROM langgraph_service.task_agent_roster_snapshot" in query:
                self.row = state["tasks"].get(parameters[0])
            elif "FOR SHARE" in query:
                state["active_reads"] += 1
                self.row = state["active"]
            elif query.startswith("UPDATE langgraph_service.agent_roster"):
                document, updated_by, expected_revision = parameters
                active = state["active"]
                if active["revision"] != expected_revision:
                    self.row = None
                else:
                    active.update(
                        roster_document=json.loads(document),
                        revision=active["revision"] + 1,
                        updated_at="now",
                        updated_by=updated_by,
                    )
                    self.row = active
            elif query.startswith(
                "INSERT INTO langgraph_service.task_agent_roster_snapshot"
            ):
                task_id, revision, profiles = parameters
                state["tasks"].setdefault(
                    task_id,
                    {
                        "roster_revision": revision,
                        "profile_snapshot": json.loads(profiles),
                    },
                )
                self.row = None
            else:
                raise AssertionError(f"unexpected query: {query}")

        def fetchone(self):
            return self.row

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def cursor(self):
            return Cursor()

    def connect(_database_url, *, row_factory):
        assert row_factory is not None
        return Connection()

    store = PostgresAgentRosterStore("postgresql://test", connection_factory=connect)
    first = store.update(_roster("v1"), expected_revision=0, updated_by="owner")
    started = store.resolve_for_task("task-1")
    store.update(_roster("v2"), expected_revision=first.revision, updated_by="owner")
    active_reads = state["active_reads"]
    resumed = store.resolve_for_task("task-1")

    assert first.revision == 1
    assert started["revision"] == 1
    assert started["profiles"]["context"]["prompt_content"] == "Follow the v1 profile."
    assert resumed == started
    assert state["active_reads"] == active_reads
