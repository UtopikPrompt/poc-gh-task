from contextlib import nullcontext
from datetime import datetime, timezone

from langgraph.checkpoint.memory import InMemorySaver
from langgraph_service.agent_config import AgentConfig
from langgraph_service.storage.config_store import AgentConfigSnapshot
from langgraph_service.workflow.operations import WorkflowOperations
from langgraph_service.workflow.service import LangGraphTaskService
from langgraph_service.workflow.task_graph import TASK_GRAPH_NODE_NAMES


class MutableRosterStore:
    def __init__(self):
        self.current = {
            "revision": 1,
            "profiles": {"context": {"agent_id": "context-v1"}},
        }
        self.snapshots = {}

    def resolve_for_task(self, task_id):
        if task_id not in self.snapshots:
            self.snapshots[task_id] = self.current
        return self.snapshots[task_id]


def test_langgraph_service_uses_postgres_saver_factory():
    calls = []

    class FakeSaver(InMemorySaver):
        def setup(self):
            calls.append("setup")

    class FakePostgresSaver:
        @classmethod
        def from_conn_string(cls, connection_string):
            calls.append(connection_string)
            return nullcontext(FakeSaver())

    with LangGraphTaskService(
        database_url="postgresql://configured-by-operator",
        postgres_saver_factory=FakePostgresSaver,
        agent_roster_store=MutableRosterStore(),
    ) as service:
        result = service.invoke({"task_id": "task-1", "event_type": "unknown"})

    assert calls == ["postgresql://configured-by-operator"]
    assert result["workflow_status"] == "invalid_event"


def test_agent_config_refreshes_on_ready_for_resumed_thread():
    class FakeSaver:
        @classmethod
        def from_conn_string(cls, _connection_string):
            return nullcontext(InMemorySaver())

    class ConfigStore:
        current = AgentConfigSnapshot(
            AgentConfig(completion_timeout_seconds=60),
            1,
            datetime.now(timezone.utc),
            "owner",
        )
        reads = 0

        def get(self):
            self.reads += 1
            return self.current

    config_store = ConfigStore()
    with LangGraphTaskService(
        database_url="postgresql://configured-by-operator",
        postgres_saver_factory=FakeSaver,
        operations=WorkflowOperations(),
        agent_config_store=config_store,
        agent_roster_store=MutableRosterStore(),
    ) as service:
        started = service.invoke(
            {"task_id": "task-config", "task_status": "ready", "event_type": "ready"}
        )
        config_store.current = AgentConfigSnapshot(
            AgentConfig(completion_timeout_seconds=75),
            2,
            datetime.now(timezone.utc),
            "owner",
        )
        resumed = service.invoke({"task_id": "task-config", "event_type": "ci"})
        duplicate_ready = service.invoke(
            {"task_id": "task-config", "task_status": "ready", "event_type": "ready"}
        )

        assert started["agent_config"] == {
            "completion_timeout_seconds": 60,
        }
        assert resumed["agent_config"] == {
            "completion_timeout_seconds": 60,
        }
        assert duplicate_ready["agent_config"] == {
            "completion_timeout_seconds": 75,
        }
    assert config_store.reads == 2


def test_task_resume_keeps_roster_revision_and_profiles_from_task_start():
    class FakeSaver:
        @classmethod
        def from_conn_string(cls, _connection_string):
            return nullcontext(InMemorySaver())

    roster_store = MutableRosterStore()
    with LangGraphTaskService(
        database_url="postgresql://configured-by-operator",
        postgres_saver_factory=FakeSaver,
        operations=WorkflowOperations(),
        agent_roster_store=roster_store,
    ) as service:
        started = service.invoke(
            {
                "task_id": "task-roster",
                "task_status": "ready",
                "event_type": "ready",
                "target_repositories": ["example/project"],
            }
        )
        roster_store.current = {
            "revision": 2,
            "profiles": {"context": {"agent_id": "context-v2"}},
        }
        resumed = service.invoke(
            {
                "task_id": "task-roster",
                "event_type": "ci",
                "agent_roster_snapshot": {
                    "revision": 99,
                    "profiles": {"context": {"agent_id": "caller-override"}},
                },
            }
        )
        duplicate_ready = service.invoke(
            {
                "task_id": "task-roster",
                "task_status": "ready",
                "event_type": "ready",
                "target_repositories": ["example/project"],
            }
        )

    expected = {"revision": 1, "profiles": {"context": {"agent_id": "context-v1"}}}
    assert started["agent_roster_snapshot"] == expected
    assert resumed["agent_roster_snapshot"] == expected
    assert duplicate_ready["agent_roster_snapshot"] == expected


def test_execution_detail_exposes_only_known_next_graph_nodes():
    class FakeSaver:
        @classmethod
        def from_conn_string(cls, _connection_string):
            return nullcontext(InMemorySaver())

    with LangGraphTaskService(
        database_url="postgresql://configured-by-operator",
        postgres_saver_factory=FakeSaver,
        operations=WorkflowOperations(),
        agent_roster_store=MutableRosterStore(),
    ) as service:
        service.invoke(
            {
                "task_id": "task-flow",
                "task_status": "ready",
                "event_type": "ready",
                "target_repositories": ["example/project"],
            }
        )
        detail = service.get_execution("task-flow")

    assert detail is not None
    next_nodes = [
        node
        for checkpoint in detail["checkpoints"]
        for node in checkpoint["next_nodes"]
    ]
    assert "context" in next_nodes
    assert set(next_nodes) <= TASK_GRAPH_NODE_NAMES
