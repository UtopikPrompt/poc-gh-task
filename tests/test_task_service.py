from contextlib import nullcontext

from langgraph.checkpoint.memory import InMemorySaver

from codefactory.langgraph_service.service import LangGraphTaskService


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

    assert calls == ["postgresql://configured-by-operator", "setup"]
    assert result["workflow_status"] == "invalid_event"
