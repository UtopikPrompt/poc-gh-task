from datetime import datetime, timezone

import pytest

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
                "rollout_status": "not_requested",
                "updated_at": None,
                "updated_by": None,
            },
            "cpu": {
                "instance_id": "cpu",
                "model_filename": CPU_MODEL,
                "version": 0,
                "rollout_status": "not_requested",
                "updated_at": None,
                "updated_by": None,
            },
            "gpu-secondary": {
                "instance_id": "gpu-secondary",
                "model_filename": GPU_MODEL,
                "version": 0,
                "rollout_status": "not_requested",
                "updated_at": None,
                "updated_by": None,
            },
            "cpu-secondary": {
                "instance_id": "cpu-secondary",
                "model_filename": CPU_MODEL,
                "version": 0,
                "rollout_status": "not_requested",
                "updated_at": None,
                "updated_by": None,
            },
        },
        "query": "",
        "queries": [],
        "outbox": [],
        "parameters": (),
    }

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, query, parameters=()):
            state["query"] = query
            state["queries"].append(query)
            state["parameters"] = parameters
            if query.startswith("INSERT"):
                state["outbox"].append(parameters)
            if query.startswith("UPDATE"):
                filename, version, rollout_request_id, login, instance = parameters
                row = state["rows"][instance]
                row.update(
                    model_filename=filename,
                    version=version,
                    rollout_status="pending",
                    rollout_request_id=rollout_request_id,
                    updated_at=datetime.now(timezone.utc),
                    updated_by=login,
                )

        def fetchall(self):
            return list(state["rows"].values())

        def fetchone(self):
            query = state["query"]
            if query.startswith("SELECT"):
                return state["rows"].get(state["parameters"][0])
            instance = state["parameters"][-1]
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
    assert initial["gpu-secondary"].config.model_filename == GPU_MODEL
    assert initial["cpu-secondary"].config.model_filename == CPU_MODEL
    assert saved.version == 1
    assert saved.updated_by == "owner"
    assert saved.config.rollout_status == "pending"
    assert saved.config.rollout_request_id is not None
    assert current["gpu"].config.model_filename == "new-model.gguf"
    assert current["cpu"].config.model_filename == CPU_MODEL
    assert current["gpu-secondary"].config.model_filename == GPU_MODEL
    assert current["cpu-secondary"].config.model_filename == CPU_MODEL
    update_query = next(
        query for query in state["queries"] if query.startswith("UPDATE")
    )
    assert "WHERE instance_id = %s" in update_query
    assert "rollout_status NOT IN ('pending', 'rolling_out')" in update_query
    assert any("inference_model_rollout_outbox" in query for query in state["queries"])
    assert state["outbox"][0][2:] == (
        "gpu",
        "new-model.gguf",
        1,
        f"gpu:1:{saved.config.rollout_request_id}",
    )

    with pytest.raises(ValueError, match="inference model rollout is active"):
        store.update(
            InferenceInstanceConfig(instance_id="gpu", model_filename="new-model.gguf"),
            updated_by="owner",
        )

    state["rows"]["gpu"]["rollout_status"] = "not_requested"
    reapplied = store.update(
        InferenceInstanceConfig(instance_id="gpu", model_filename="new-model.gguf"),
        updated_by="owner",
    )
    assert reapplied.version == 2
    assert reapplied.config.rollout_status == "pending"
    assert len(state["outbox"]) == 2

    state["rows"]["gpu"]["rollout_status"] = "failed"
    retried = store.update(
        InferenceInstanceConfig(instance_id="gpu", model_filename="new-model.gguf"),
        updated_by="owner",
    )
    assert retried.version == 3
    assert retried.config.rollout_status == "pending"
    assert len(state["outbox"]) == 3


def test_inference_config_store_creates_and_soft_deletes_instances_safely():
    state = {
        "rows": {
            "gpu": {
                "instance_id": "gpu",
                "model_filename": GPU_MODEL,
                "version": 0,
                "rollout_status": "not_requested",
                "download_status": "ready",
                "updated_at": None,
                "updated_by": None,
                "is_active": True,
            },
            "cpu": {
                "instance_id": "cpu",
                "model_filename": CPU_MODEL,
                "version": 0,
                "rollout_status": "not_requested",
                "download_status": "ready",
                "updated_at": None,
                "updated_by": None,
                "is_active": True,
            },
        },
        "query": "",
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
            if query.startswith("INSERT INTO"):
                instance_id, filename, login = parameters
                state["rows"][instance_id] = {
                    "instance_id": instance_id,
                    "model_filename": filename,
                    "version": 1,
                    "rollout_status": "not_requested",
                    "rollout_request_id": None,
                    "rollout_error": None,
                    "download_status": "ready",
                    "updated_at": datetime.now(timezone.utc),
                    "updated_by": login,
                    "is_active": True,
                }
            elif query.startswith("UPDATE"):
                state["rows"][parameters[0]]["is_active"] = False

        def fetchall(self):
            return [row for row in state["rows"].values() if row["is_active"]]

        def fetchone(self):
            query = state["query"]
            if "count(*)" in query:
                return {
                    "active_count": sum(
                        row["is_active"] for row in state["rows"].values()
                    )
                }
            instance_id = state["parameters"][0]
            row = state["rows"].get(instance_id)
            if "AND is_active" in query and row is not None and not row["is_active"]:
                return None
            return row

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
    created = store.create(
        InferenceInstanceConfig(instance_id="gpu-extra", model_filename=GPU_MODEL),
        created_by="owner",
    )

    assert created.version == 1
    assert created.updated_by == "owner"
    assert created.config.rollout_status == "not_requested"
    with pytest.raises(ValueError, match="inference instance already exists"):
        store.create(
            InferenceInstanceConfig(instance_id="gpu-extra", model_filename=GPU_MODEL),
            created_by="owner",
        )

    state["rows"]["gpu-extra"]["download_status"] = "downloading"
    with pytest.raises(ValueError, match="inference model download is active"):
        store.delete("gpu-extra")
    state["rows"]["gpu-extra"]["download_status"] = "ready"
    state["rows"]["gpu-extra"]["rollout_status"] = "pending"
    with pytest.raises(ValueError, match="inference model rollout is active"):
        store.delete("gpu-extra")
    state["rows"]["gpu-extra"]["rollout_status"] = "not_requested"
    store.delete("gpu-extra")

    assert "gpu-extra" not in store.get_all()
    assert state["rows"]["gpu-extra"]["is_active"] is False
    with pytest.raises(ValueError, match="unknown inference instance"):
        store.update(
            InferenceInstanceConfig(instance_id="gpu-extra", model_filename=GPU_MODEL),
            updated_by="owner",
        )
    store.delete("gpu")
    with pytest.raises(ValueError, match="at least one inference instance must remain"):
        store.delete("cpu")
