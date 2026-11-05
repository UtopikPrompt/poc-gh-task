import asyncio
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime, timezone
import json
import sys
from types import ModuleType, SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
import pytest
from langgraph_service.storage import migrate
from langgraph_service.agent_config import AgentConfig
from langgraph_service.inference_config import InferenceInstanceConfig
from langgraph_service.storage.agent_roster_store import AgentRosterSnapshot
from langgraph_service.storage.config_store import AgentConfigSnapshot
from langgraph_service.storage.inference_config_store import InferenceInstanceSnapshot
from langgraph_service.storage.database import postgres_url_from_environment
from langgraph_service import host
from langgraph_service.workflow.operations import WorkflowOperations
from langgraph_service.workflow.service import LangGraphTaskService
from langgraph_service.workflow.workflow_operations import make_operations
from sqlalchemy.engine import make_url


def _configure(monkeypatch, module_name):
    for key, value in {
        "LANGGRAPH_DB_HOST": "postgres.example.test",
        "LANGGRAPH_DB_PORT": "5432",
        "LANGGRAPH_DB_NAME": "langgraph",
        "LANGGRAPH_DB_USERNAME": "langgraph-user",
        "LANGGRAPH_DB_PASSWORD": "langgraph-secret",
        "LANGGRAPH_DB_SSLMODE": "verify-full",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GITHUB_SERVICE_URL", "http://github-service:8000")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "service-token")
    monkeypatch.setenv("MODEL_RECONCILER_TOKEN", "rollout-token")
    monkeypatch.setenv("WORKFLOW_OPERATIONS_MODULE", module_name)


def _roster_payload():
    roles = {
        "context": "gather_context",
        "planning": "create_plan",
        "risk": "assess_risk",
        "implementation": "prepare_changes",
        "repair": "repair_changes",
    }
    return {
        "agents": [
            {
                "agent_id": role,
                "name": f"{role} specialist",
                "description": f"Handles the {role} stage.",
                "prompt_content": f"Focus on {role} work.",
                "model_profile": "local_llama_cpp",
                "skill_ids": [],
                "tool_profile": tool,
            }
            for role, tool in roles.items()
        ],
        "assignments": {role: role for role in roles},
    }


def test_langgraph_host_requires_separate_database_and_service_contract(monkeypatch):
    for suffix in ("HOST", "PORT", "NAME", "USERNAME", "PASSWORD", "SSLMODE"):
        monkeypatch.delenv(f"LANGGRAPH_DB_{suffix}", raising=False)
    monkeypatch.delenv("GITHUB_SERVICE_URL", raising=False)
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("WORKFLOW_OPERATIONS_MODULE", raising=False)

    with pytest.raises(ValueError, match="LANGGRAPH_DB_HOST"):
        host.create_app()


def test_langgraph_host_does_not_fallback_to_database_url(monkeypatch):
    monkeypatch.setenv("LANGGRAPH_DATABASE_URL", "postgresql://legacy/db")
    for suffix in ("HOST", "PORT", "NAME", "USERNAME", "PASSWORD", "SSLMODE"):
        monkeypatch.delenv(f"LANGGRAPH_DB_{suffix}", raising=False)
    monkeypatch.setenv("GITHUB_SERVICE_URL", "http://github-service:8000")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "service-token")
    monkeypatch.setenv("WORKFLOW_OPERATIONS_MODULE", "unused_worker_module")

    with pytest.raises(ValueError, match="LANGGRAPH_DB_HOST"):
        host.create_app()


def test_langgraph_host_requires_database_username(monkeypatch):
    _configure(monkeypatch, "unused_worker_module")
    monkeypatch.delenv("LANGGRAPH_DB_USERNAME")

    with pytest.raises(ValueError, match="LANGGRAPH_DB_USERNAME"):
        host.create_app()


def test_langgraph_database_credentials_are_service_specific_and_structured(
    monkeypatch,
):
    _configure(monkeypatch, "unused_worker_module")

    parsed = make_url(postgres_url_from_environment("LANGGRAPH", required=True))

    assert parsed.username == "langgraph-user"
    assert parsed.password == "langgraph-secret"
    assert parsed.database == "langgraph"


def test_migration_admin_url_uses_structured_database_settings(monkeypatch):
    for key, value in {
        "POSTGRES_ADMIN_USERNAME": "codefactory_admin",
        "POSTGRES_ADMIN_PASSWORD": "admin:password/with?reserved",
        "GITHUB_DB_HOST": "codefactory-postgres",
        "GITHUB_DB_PORT": "5432",
        "GITHUB_DB_NAME": "codefactory",
        "GITHUB_DB_SSLMODE": "disable",
    }.items():
        monkeypatch.setenv(key, value)

    parsed = make_url(migrate._admin_url())

    assert parsed.username == "codefactory_admin"
    assert parsed.password == "admin:password/with?reserved"
    assert parsed.host == "codefactory-postgres"
    assert parsed.database == "codefactory"


def test_langgraph_host_requires_operator_operations_factory(monkeypatch):
    module = ModuleType("worker_without_factory")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    _configure(monkeypatch, module.__name__)

    with pytest.raises(TypeError, match="must export make_operations"):
        host.create_app()


def test_langgraph_host_composes_standalone_worker_app(monkeypatch):
    module = ModuleType("configured_worker")
    module.make_operations = lambda: WorkflowOperations()
    monkeypatch.setitem(sys.modules, module.__name__, module)
    _configure(monkeypatch, module.__name__)

    app = host.create_app()

    assert any(route.path == "/healthz" for route in app.routes)


def test_langgraph_host_uses_bundled_operations_factory_by_default(monkeypatch):
    _configure(monkeypatch, "unused_worker_module")
    monkeypatch.delenv("WORKFLOW_OPERATIONS_MODULE")

    app = host.create_app()
    operations = make_operations(github_client=object(), proposal_provider=object())

    assert any(route.path == "/healthz" for route in app.routes)
    assert all(
        callable(operation)
        for operation in (
            operations.gather_context,
            operations.create_plan,
            operations.assess_risk,
            operations.prepare_changes,
            operations.repair_changes,
        )
    )


def test_langgraph_readiness_exposes_local_model_unavailability(monkeypatch):
    _configure(monkeypatch, "unused_worker_module")
    monkeypatch.delenv("WORKFLOW_OPERATIONS_MODULE")
    app = host.create_app()
    app.state.proposal_provider = type(
        "UnavailableProvider", (), {"is_ready": lambda self: False}
    )()
    endpoint = next(route.endpoint for route in app.routes if route.path == "/readyz")

    response = asyncio.run(endpoint())

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "status": "unavailable",
        "dependency": "local_model",
    }


def test_private_management_api_is_authenticated_and_projects_safe_state(monkeypatch):
    _configure(monkeypatch, "unused_worker_module")
    monkeypatch.delenv("WORKFLOW_OPERATIONS_MODULE")

    class ConfigStore:
        current = AgentConfigSnapshot(AgentConfig(), 0, None, None)

        def __init__(self, _database_url):
            pass

        def get(self):
            return self.current

        def update(self, config, *, updated_by):
            self.current = AgentConfigSnapshot(
                config,
                self.current.version + 1,
                datetime.now(timezone.utc),
                updated_by,
            )
            return self.current

    class RosterStore:
        current = AgentRosterSnapshot(None, 0, None, None)

        def __init__(self, _database_url):
            pass

        def get(self):
            return self.current

        def update(self, roster, *, expected_revision, updated_by):
            if expected_revision != self.current.revision:
                raise ValueError("active roster revision changed")
            self.current = AgentRosterSnapshot(
                roster,
                self.current.revision + 1,
                datetime.now(timezone.utc),
                updated_by,
            )
            return self.current

    class InferenceConfigStore:
        current = {
            "gpu": InferenceInstanceSnapshot(
                InferenceInstanceConfig(
                    instance_id="gpu",
                    model_filename="qwen2.5-32b-instruct-q4_k_m.gguf",
                    rollout_status="failed",
                    rollout_error="credential-marker",
                ),
                0,
                None,
                None,
            ),
            "cpu": InferenceInstanceSnapshot(
                InferenceInstanceConfig(
                    instance_id="cpu",
                    model_filename="qwen2.5-0.5b-instruct-q4_k_m.gguf",
                ),
                0,
                None,
                None,
            ),
        }

        def __init__(self, _database_url):
            pass

        def get_all(self):
            return self.current

        def update(self, config, *, updated_by):
            previous = self.current[config.instance_id]
            self.current[config.instance_id] = InferenceInstanceSnapshot(
                config,
                previous.version + 1,
                datetime.now(timezone.utc),
                updated_by,
            )
            return self.current[config.instance_id]

    class ModelDownloadStore:
        request = SimpleNamespace(
            request_id=UUID("00000000-0000-0000-0000-000000000001"),
            instance_id="gpu",
            model_filename="model.gguf",
            model_url="https://huggingface.co/org/repo/model.gguf?token=private-marker",
            model_sha256="a" * 64,
            requested_by="owner-login",
            status="queued",
            public_error=None,
            attempts=0,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        def __init__(self, _database_url):
            pass

        def submit(self, **_kwargs):
            return self.request

        def get(self, _request_id):
            return SimpleNamespace(
                **{
                    **vars(self.request),
                    "status": "failed",
                    "public_error": "credential-marker",
                }
            )

        def claim_rollout(self, *, lease_seconds):
            assert lease_seconds == 120
            return SimpleNamespace(
                event_id=UUID("00000000-0000-0000-0000-000000000002"),
                instance_id="gpu",
                download_request_id=self.request.request_id,
                model_filename="model.gguf",
                config_version=1,
                idempotency_key="gpu:1:00000000-0000-0000-0000-000000000001",
                lease_token=UUID("00000000-0000-0000-0000-000000000003"),
                lease_until=datetime.now(timezone.utc),
                attempts=1,
            )

        def acknowledge_rollout(self, event_id, lease_token, *, outcome):
            assert event_id == UUID("00000000-0000-0000-0000-000000000002")
            assert lease_token == UUID("00000000-0000-0000-0000-000000000003")
            assert outcome == "succeeded"
            return outcome

    class FakeSaver:
        @classmethod
        def from_conn_string(cls, _database_url):
            return nullcontext(InMemorySaver())

    monkeypatch.setattr(host, "PostgresAgentConfigStore", ConfigStore)
    monkeypatch.setattr(
        host, "PostgresInferenceInstanceConfigStore", InferenceConfigStore
    )
    monkeypatch.setattr(host, "PostgresModelDownloadStore", ModelDownloadStore)
    monkeypatch.setattr(host, "PostgresAgentRosterStore", RosterStore)
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    app = host.create_app()

    @asynccontextmanager
    async def no_lifespan(_app):
        yield

    app.router.lifespan_context = no_lifespan
    with LangGraphTaskService(
        database_url="postgresql://test",
        operations=WorkflowOperations(),
        postgres_saver_factory=FakeSaver,
    ) as lifecycle:
        lifecycle.invoke(
            {
                "task_id": "task-safe",
                "event_type": "unknown",
                "issue": {"body": "private-issue-payload-marker"},
                "context": {"files": [{"content": "private-source-payload-marker"}]},
            }
        )
        app.state.task_lifecycle = lifecycle
        with TestClient(app) as client:
            assert client.get("/internal/management/agent-config").status_code == 401
            assert client.get("/internal/management/agent-roster").status_code == 401
            assert (
                client.get("/internal/management/inference-instances").status_code
                == 401
            )
            headers = {"Authorization": "Bearer service-token"}
            initial = client.get("/internal/management/agent-config", headers=headers)
            updated = client.put(
                "/internal/management/agent-config",
                headers=headers,
                json={
                    "completion_timeout_seconds": 60,
                    "updated_by": "owner-login",
                },
            )
            invalid = client.put(
                "/internal/management/agent-config",
                headers=headers,
                json={
                    "completion_timeout_seconds": 60,
                    "updated_by": "owner-login",
                    "api_token": "secret-marker",
                },
            )
            initial_instances = client.get(
                "/internal/management/inference-instances", headers=headers
            )
            unauthenticated_download = client.post(
                "/internal/management/inference-instances/gpu/model-downloads",
                json={
                    "model_filename": "model.gguf",
                    "model_url": "https://huggingface.co/org/repo/model.gguf",
                    "model_sha256": "a" * 64,
                    "requested_by": "owner-login",
                },
            )
            rejected_download = client.post(
                "/internal/management/inference-instances/gpu/model-downloads",
                headers=headers,
                json={
                    "model_filename": "model.gguf",
                    "model_url": "https://user:credential-marker@huggingface.co/model",
                    "model_sha256": "a" * 64,
                    "requested_by": "owner-login",
                },
            )
            malformed_download = client.post(
                "/internal/management/inference-instances/gpu/model-downloads",
                headers=headers,
                json={
                    "model_filename": "model.gguf",
                    "model_url": "https://huggingface.co/model?token=credential-marker",
                    "requested_by": "owner-login",
                },
            )
            submitted_download = client.post(
                "/internal/management/inference-instances/gpu/model-downloads",
                headers=headers,
                json={
                    "model_filename": "model.gguf",
                    "model_url": "https://huggingface.co/org/repo/model.gguf",
                    "model_sha256": "a" * 64,
                    "requested_by": "owner-login",
                },
            )
            download_status = client.get(
                "/internal/management/inference-model-downloads/00000000-0000-0000-0000-000000000001",
                headers=headers,
            )
            unauthenticated_rollout_claim = client.post(
                "/internal/reconciler/inference-model-rollouts/claim",
                json={"lease_seconds": 120},
            )
            wrong_token_rollout_claim = client.post(
                "/internal/reconciler/inference-model-rollouts/claim",
                headers=headers,
                json={"lease_seconds": 120},
            )
            reconciler_headers = {"Authorization": "Bearer rollout-token"}
            rollout_claim = client.post(
                "/internal/reconciler/inference-model-rollouts/claim",
                headers=reconciler_headers,
                json={"lease_seconds": 120},
            )
            rollout_ack = client.post(
                "/internal/reconciler/inference-model-rollouts/00000000-0000-0000-0000-000000000002/ack",
                headers=reconciler_headers,
                json={
                    "lease_token": "00000000-0000-0000-0000-000000000003",
                    "outcome": "succeeded",
                },
            )
            metrics = client.get(
                "/internal/management/inference-metrics", headers=headers
            )
            updated_instance = client.put(
                "/internal/management/inference-instances/gpu",
                headers=headers,
                json={
                    "instance_id": "gpu",
                    "model_filename": "new-gpu-model.gguf",
                    "updated_by": "owner-login",
                },
            )
            initial_roster = client.get(
                "/internal/management/agent-roster", headers=headers
            )
            updated_roster = client.put(
                "/internal/management/agent-roster",
                headers=headers,
                json={
                    "roster": _roster_payload(),
                    "expected_revision": 0,
                    "updated_by": "owner-login",
                },
            )
            unsupported_profile = _roster_payload()
            unsupported_profile["agents"][0]["tool_profile"] = "shell"
            invalid_roster = client.put(
                "/internal/management/agent-roster",
                headers=headers,
                json={
                    "roster": unsupported_profile,
                    "expected_revision": 1,
                    "updated_by": "owner-login",
                },
            )
            stale_roster = client.put(
                "/internal/management/agent-roster",
                headers=headers,
                json={
                    "roster": _roster_payload(),
                    "expected_revision": 0,
                    "updated_by": "owner-login",
                },
            )
            listing = client.get("/internal/management/executions", headers=headers)
            detail = client.get(
                "/internal/management/executions/task-safe", headers=headers
            )

    assert initial.status_code == 200
    assert initial.json() == {
        "config": {
            "completion_timeout_seconds": 45,
        },
        "version": 0,
        "updated_at": None,
        "updated_by": None,
    }
    assert updated.status_code == 200
    assert updated.json()["config"] == {
        "completion_timeout_seconds": 60,
    }
    assert updated.json()["version"] == 1
    assert updated.json()["updated_by"] == "owner-login"
    assert initial_instances.status_code == updated_instance.status_code == 200
    assert initial_instances.json()["instances"][0]["config"]["rollout_error"] == (
        "Model rollout failed; verify the inference deployment and retry."
    )
    assert "credential-marker" not in initial_instances.text
    assert unauthenticated_download.status_code == 401
    assert rejected_download.status_code == 422
    assert "credential-marker" not in rejected_download.text
    assert malformed_download.status_code == 422
    assert "credential-marker" not in malformed_download.text
    assert submitted_download.status_code == 202
    assert download_status.status_code == 200
    assert "model_url" not in submitted_download.json()
    assert "private-marker" not in submitted_download.text
    assert download_status.json()["public_error"] == (
        "Model download failed; verify artifact availability and retry."
    )
    assert "credential-marker" not in download_status.text
    assert unauthenticated_rollout_claim.status_code == 401
    assert wrong_token_rollout_claim.status_code == 401
    assert rollout_claim.status_code == 200
    assert rollout_claim.json()["event"]["instance_id"] == "gpu"
    assert rollout_claim.json()["event"]["model_filename"] == "model.gguf"
    assert rollout_claim.json()["event"]["config_version"] == 1
    assert rollout_claim.json()["event"]["idempotency_key"].startswith("gpu:1:")
    assert rollout_ack.status_code == 200
    assert rollout_ack.json()["status"] == "succeeded"
    assert metrics.status_code == 200
    assert [item["instance_id"] for item in metrics.json()["instances"]] == [
        "gpu",
        "cpu",
    ]
    assert all(
        item["cpu_utilization_percent"]["status"] == "unavailable"
        for item in metrics.json()["instances"]
    )
    assert [
        item["config"]["model_filename"]
        for item in initial_instances.json()["instances"]
    ] == [
        "qwen2.5-32b-instruct-q4_k_m.gguf",
        "qwen2.5-0.5b-instruct-q4_k_m.gguf",
    ]
    assert updated_instance.json()["config"]["model_filename"] == "new-gpu-model.gguf"
    assert InferenceConfigStore.current["cpu"].config.model_filename == (
        "qwen2.5-0.5b-instruct-q4_k_m.gguf"
    )
    assert invalid.status_code == 422
    assert initial_roster.status_code == 200
    assert initial_roster.json() == {
        "roster": None,
        "revision": 0,
        "updated_at": None,
        "updated_by": None,
    }
    assert updated_roster.status_code == 200
    assert updated_roster.json()["revision"] == 1
    assert invalid_roster.status_code == 422
    assert stale_roster.status_code == 409
    assert listing.status_code == detail.status_code == 200
    assert listing.json()["executions"][0]["thread_id"] == "task-safe"
    assert detail.json()["thread_id"] == "task-safe"
    assert "private-issue-payload-marker" not in listing.text + detail.text
    assert "private-source-payload-marker" not in listing.text + detail.text
    assert "secret-marker" not in updated.text


def test_no_ci_resolution_endpoint_persists_authenticated_operator_audit(monkeypatch):
    _configure(monkeypatch, "unused_worker_module")
    monkeypatch.delenv("WORKFLOW_OPERATIONS_MODULE")

    class ConfigStore:
        def __init__(self, _database_url):
            pass

    class FakeSaver:
        @classmethod
        def from_conn_string(cls, _database_url):
            return nullcontext(InMemorySaver())

    monkeypatch.setattr(host, "PostgresAgentConfigStore", ConfigStore)
    app = host.create_app()

    @asynccontextmanager
    async def no_lifespan(_app):
        yield

    app.router.lifespan_context = no_lifespan
    operations = WorkflowOperations(
        gather_context=lambda _: {},
        create_plan=lambda _: {},
        assess_risk=lambda _: {},
        prepare_changes=lambda _: {},
    )

    class RosterStore:
        def resolve_for_task(self, _task_id):
            return {"revision": 1, "profiles": {}}

    with LangGraphTaskService(
        database_url="postgresql://test",
        operations=operations,
        publish_pr=lambda _: {
            "pull_request_id": "pr-17",
            "head_sha": "head-1",
            "ci_head_sha": "head-1",
            "ci_checks": [],
            "ci_complete": True,
        },
        postgres_saver_factory=FakeSaver,
        agent_roster_store=RosterStore(),
    ) as lifecycle:
        ready = lifecycle.invoke(
            {
                "task_id": "task-no-ci",
                "task_status": "ready",
                "event_type": "ready",
                "target_repositories": ["example/project"],
            }
        )
        assert ready["workflow_status"] == "no_ci_checks"
        app.state.task_lifecycle = lifecycle
        with TestClient(app) as client:
            path = "/internal/management/executions/task-no-ci/resolve-no-ci"
            body = {"actor": "verified-owner", "decision": "proceed_without_ci"}
            unauthorized = client.post(path, json=body)
            resolved = client.post(
                path,
                headers={"Authorization": "Bearer service-token"},
                json=body,
            )
            duplicate = client.post(
                path,
                headers={"Authorization": "Bearer service-token"},
                json=body,
            )
            detail = client.get(
                "/internal/management/executions/task-no-ci",
                headers={"Authorization": "Bearer service-token"},
            )

    assert unauthorized.status_code == 401
    assert resolved.status_code == 200
    assert resolved.json() == {"resolved": True}
    assert duplicate.status_code == 409
    audit = detail.json()["checkpoints"][0]["no_ci_resolution"]
    assert audit["actor"] == "verified-owner"
    assert audit["decision"] == "proceed_without_ci"
    assert audit["pull_request_id"] == "pr-17"
    assert audit["decided_at"].startswith("2026-11-")
    assert any(
        checkpoint["event_type"] == "resolve_no_ci"
        and checkpoint["event_status"] == "no_ci_resolved"
        for checkpoint in detail.json()["checkpoints"]
    )
