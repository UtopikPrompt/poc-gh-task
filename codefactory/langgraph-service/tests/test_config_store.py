from datetime import datetime, timezone

from langgraph_service.agent_config import AgentConfig
from langgraph_service.config_store import PostgresAgentConfigStore


def test_postgres_config_store_uses_defaults_and_persists_audited_override():
    state = {"row": None, "queries": []}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, query, parameters=()):
            state["queries"].append((query, parameters))
            if query.startswith("INSERT"):
                previous = state["row"]
                state["row"] = {
                    "completion_timeout_seconds": parameters[0],
                    "version": 1 if previous is None else previous["version"] + 1,
                    "updated_at": datetime.now(timezone.utc),
                    "updated_by": parameters[1],
                }

        def fetchone(self):
            return state["row"]

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

    store = PostgresAgentConfigStore("postgresql://test", connection_factory=connect)

    initial = store.get()
    saved = store.update(AgentConfig(completion_timeout_seconds=60), updated_by="owner")
    current = store.get()

    assert initial.config == AgentConfig()
    assert initial.version == 0
    assert initial.updated_at is initial.updated_by is None
    assert saved.config.completion_timeout_seconds == 60
    assert saved.version == 1
    assert saved.updated_by == "owner"
    assert current == saved
    update_query, parameters = state["queries"][1]
    assert "ON CONFLICT (id) DO UPDATE" in update_query
    assert "version = langgraph_service.agent_config.version + 1" in update_query
    assert parameters == (60, "owner")
