from datetime import datetime, timezone
from uuid import uuid4

import pytest

from langgraph_service.inference_config import InferenceInstanceConfig
from langgraph_service.storage.inference_config_store import (
    PostgresInferenceInstanceConfigStore,
)


GPU_MODEL = "qwen2.5-32b-instruct-q4_k_m.gguf"
CPU_MODEL = "qwen2.5-0.5b-instruct-q4_k_m.gguf"


def test_inference_runtime_options_respect_cpu_and_gpu_profiles():
    gpu = InferenceInstanceConfig(
        instance_id="gpu-new",
        model_filename=GPU_MODEL,
        context_size=16384,
        threads=8,
    )
    assert gpu.context_size == 16384
    assert gpu.threads == 8

    with pytest.raises(ValueError, match="CPU instance profile limit"):
        InferenceInstanceConfig(
            instance_id="cpu-new",
            model_filename=CPU_MODEL,
            context_size=8192,
            threads=2,
        )
    with pytest.raises(ValueError, match="CPU instance profile limit"):
        InferenceInstanceConfig(
            instance_id="cpu-new",
            model_filename=CPU_MODEL,
            context_size=1024,
            threads=5,
        )


def test_inference_config_store_reads_both_instances_and_updates_only_one():
    state = {
        "rows": {
            "gpu": {
                "instance_id": "gpu",
                "model_filename": GPU_MODEL,
                "version": 0,
                "rollout_status": "not_requested",
                "workload_status": "ready",
                "updated_at": None,
                "updated_by": None,
            },
            "cpu": {
                "instance_id": "cpu",
                "model_filename": CPU_MODEL,
                "version": 0,
                "rollout_status": "not_requested",
                "workload_status": "ready",
                "updated_at": None,
                "updated_by": None,
            },
            "gpu-secondary": {
                "instance_id": "gpu-secondary",
                "model_filename": GPU_MODEL,
                "version": 0,
                "rollout_status": "not_requested",
                "workload_status": "ready",
                "updated_at": None,
                "updated_by": None,
            },
            "cpu-secondary": {
                "instance_id": "cpu-secondary",
                "model_filename": CPU_MODEL,
                "version": 0,
                "rollout_status": "not_requested",
                "workload_status": "ready",
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
                "workload_status": "ready",
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
                "workload_status": "ready",
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
            if "inference_instance_config" in query and query.startswith("INSERT"):
                instance_id, filename, context_size, threads, login = parameters
                state["rows"][instance_id] = {
                    "instance_id": instance_id,
                    "model_filename": filename,
                    "context_size": context_size,
                    "threads": threads,
                    "version": 1,
                    "rollout_status": "not_requested",
                    "rollout_request_id": None,
                    "rollout_error": None,
                    "download_status": "ready",
                    "updated_at": datetime.now(timezone.utc),
                    "updated_by": login,
                    "is_active": True,
                    "workload_status": "provisioning",
                }
            elif query.startswith("UPDATE") and "SET model_filename" in query:
                instance_id = parameters[-1]
                state["rows"][instance_id].update(
                    model_filename=parameters[0],
                    context_size=parameters[1],
                    threads=parameters[2],
                    version=2,
                    updated_by=parameters[3],
                    is_active=True,
                    workload_status="provisioning",
                )
            elif "inference_workload_outbox" in query:
                state.setdefault("workload_events", []).append(parameters)
            elif query.startswith("UPDATE"):
                row = state["rows"][parameters[0]]
                row["is_active"] = False
                row["workload_status"] = "deleting"

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
            if query.startswith("UPDATE") and "SET model_filename" in query:
                return state["rows"].get(state["parameters"][-1])
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
    assert created.config.workload_status == "provisioning"
    assert created.config.context_size == 1024
    assert created.config.threads == 2
    assert state["workload_events"][-1][1:] == ("gpu-extra",)
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
    state["rows"]["gpu-extra"]["workload_status"] = "ready"
    store.delete("gpu-extra")

    assert "gpu-extra" not in store.get_all()
    assert state["rows"]["gpu-extra"]["is_active"] is False
    assert state["rows"]["gpu-extra"]["workload_status"] == "deleting"
    with pytest.raises(ValueError, match="unknown inference instance"):
        store.update(
            InferenceInstanceConfig(instance_id="gpu-extra", model_filename=GPU_MODEL),
            updated_by="owner",
        )
    state["rows"]["gpu-extra"]["workload_status"] = "deleted"
    recreated = store.create(
        InferenceInstanceConfig(
            instance_id="gpu-extra",
            model_filename=GPU_MODEL,
            context_size=8192,
            threads=3,
        ),
        created_by="owner",
    )
    assert recreated.version == 2
    assert recreated.config.workload_status == "provisioning"
    assert recreated.config.context_size == 8192
    assert recreated.config.threads == 3
    state["rows"]["gpu-extra"]["workload_status"] = "ready"
    store.delete("gpu-extra")
    store.delete("gpu")
    store.delete("cpu")
    assert not store.get_all()


def test_inference_workload_queue_claims_and_acknowledges_with_a_lease():
    event_id = uuid4()
    lease_token = uuid4()
    state = {"query": "", "parameters": (), "queries": [], "lease_active": True}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, query, parameters=()):
            state["query"] = query
            state["parameters"] = parameters
            state["queries"].append(query)

        def fetchone(self):
            if "inference_workload_outbox AS event" in state["query"]:
                return {
                    "event_id": event_id,
                    "instance_id": "gpu-secondary",
                    "operation": "ensure",
                    "attempts": 2,
                    "model_filename": GPU_MODEL,
                    "context_size": 8192,
                    "threads": 3,
                }
            if "completed_lease_token" in state["query"]:
                return {
                    "instance_id": "gpu-secondary",
                    "operation": "ensure",
                    "status": "leased",
                    "lease_token": lease_token,
                    "completed_lease_token": None,
                    "lease_active": state["lease_active"],
                }
            return None

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

    claim = store.claim_workload(120)
    assert claim is not None
    assert claim.event_id == event_id
    assert claim.instance_id == "gpu-secondary"
    assert claim.operation == "ensure"
    assert claim.attempts == 3
    assert claim.context_size == 8192
    assert claim.threads == 3
    assert "FOR UPDATE OF event SKIP LOCKED" in state["queries"][0]
    assert state["queries"][1].find("lease_until") > 0

    assert (
        store.acknowledge_workload(event_id, lease_token, succeeded=True) == "succeeded"
    )
    assert any("completed_lease_token = %s" in query for query in state["queries"])

    state["lease_active"] = False
    with pytest.raises(ValueError, match="inference workload lease is stale"):
        store.acknowledge_workload(event_id, lease_token, succeeded=False)
