from datetime import datetime, timezone

from langgraph_service.inference_config import InferenceInstanceConfig
from langgraph_service.storage.inference_config_store import (
    PostgresInferenceInstanceConfigStore,
)


GPU_MODEL = "qwen2.5-32b-instruct-q4_k_m.gguf"
CPU_MODEL = "qwen2.5-0.5b-instruct-q4_k_m.gguf"


def test_inference_config_store_reads_both_instances_and_updates_only_one():
    state = {
        "rows": {
            "gpu": {
                "instance_id": "gpu",
                "model_filename": GPU_MODEL,
                "version": 0,
                "updated_at": None,
                "updated_by": None,
            },
            "cpu": {
                "instance_id": "cpu",
                "model_filename": CPU_MODEL,
                "version": 0,
                "updated_at": None,
                "updated_by": None,
            },
        },
        "query": "",
        "update_query": "",
        "update_parameters": (),
        "parameters": (),
    }

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, query, parameters=()):
            state["query"] = query
            state["parameters"] = parameters
            if query.startswith("UPDATE"):
                state["update_query"] = query
                state["update_parameters"] = parameters
                filename, login, instance = parameters
                row = state["rows"][instance]
                row.update(
                    model_filename=filename,
                    version=row["version"] + 1,
                    updated_at=datetime.now(timezone.utc),
                    updated_by=login,
                )

        def fetchall(self):
            return list(state["rows"].values())

        def fetchone(self):
            instance = state["parameters"][2]
            return state["rows"].get(instance)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def cursor(self):
            return Cursor()

    store = PostgresInferenceInstanceConfigStore(
        "postgresql://test", connection_factory=lambda *_args, **_kwargs: Connection()
    )

    initial = store.get_all()
    saved = store.update(
        InferenceInstanceConfig(instance_id="gpu", model_filename="new-model.gguf"),
        updated_by="owner",
    )
    current = store.get_all()

    assert initial["gpu"].config.model_filename == GPU_MODEL
    assert initial["cpu"].config.model_filename == CPU_MODEL
    assert saved.version == 1
    assert saved.updated_by == "owner"
    assert current["gpu"].config.model_filename == "new-model.gguf"
    assert current["cpu"].config.model_filename == CPU_MODEL
    assert "WHERE instance_id = %s" in state["update_query"]
    assert state["update_parameters"] == ("new-model.gguf", "owner", "gpu")
