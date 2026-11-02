from contextlib import nullcontext
from datetime import datetime, timezone

from langgraph.checkpoint.memory import InMemorySaver
from langgraph_service.agent_config import AgentConfig
from langgraph_service.config_store import AgentConfigSnapshot
from langgraph_service.operations import WorkflowOperations
from langgraph_service.service import LangGraphTaskService


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
    ) as service:
        result = service.invoke({"task_id": "task-1", "event_type": "unknown"})

    assert calls == ["postgresql://configured-by-operator"]
    assert result["workflow_status"] == "invalid_event"


def test_agent_config_is_snapshotted_once_per_workflow_thread():
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

    assert started["agent_config"] == {"completion_timeout_seconds": 60}
    assert resumed["agent_config"] == {"completion_timeout_seconds": 60}
    assert duplicate_ready["agent_config"] == {"completion_timeout_seconds": 60}
    assert config_store.reads == 1
