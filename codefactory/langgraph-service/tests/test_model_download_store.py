from datetime import datetime, timezone
from uuid import uuid4

import pytest

from langgraph_service.storage.model_download_store import (
    PUBLIC_FAILURES,
    PostgresModelDownloadStore,
    validate_download_transition,
)


def test_download_status_transition_contract_is_explicit():
    for current, target in (
        ("queued", "downloading"),
        ("downloading", "downloading"),
        ("downloading", "verifying"),
        ("verifying", "downloading"),
        ("verifying", "ready"),
        ("queued", "failed"),
        ("downloading", "failed"),
        ("verifying", "failed"),
    ):
        validate_download_transition(current, target)

    for current, target in (
        ("queued", "ready"),
        ("downloading", "ready"),
        ("ready", "downloading"),
        ("failed", "ready"),
    ):
        with pytest.raises(ValueError, match="cannot transition"):
            validate_download_transition(current, target)


def test_failure_messages_are_fixed_safe_text():
    assert set(PUBLIC_FAILURES) == {
        "hash_mismatch",
        "too_large",
        "policy_rejected",
        "download_failed",
    }
    assert all("token" not in message.lower() for message in PUBLIC_FAILURES.values())
    assert all("https://" not in message for message in PUBLIC_FAILURES.values())


def test_claim_uses_one_atomic_skip_locked_lease():
    now = datetime.now(timezone.utc)
    token_id = uuid4()
    state = {"queries": []}
    row = {
        "request_id": uuid4(),
        "instance_id": "gpu",
        "model_filename": "model.gguf",
        "model_url": "https://huggingface.co/org/repo/model.gguf",
        "model_sha256": "a" * 64,
        "requested_by": "owner",
        "status": "downloading",
        "public_error": None,
        "attempts": 1,
        "created_at": now,
        "updated_at": now,
        "lease_token": token_id,
    }

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, query, _parameters=()):
            state["queries"].append(query)

        def fetchone(self):
            return row

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def cursor(self):
            return Cursor()

    store = PostgresModelDownloadStore(
        "postgresql://test", connection_factory=lambda *_args, **_kwargs: Connection()
    )
    claimed = store.claim()

    assert claimed.request_id == row["request_id"]
    assert claimed.lease_token == token_id
    assert "FOR UPDATE SKIP LOCKED" in state["queries"][0]
    assert "lease_until < now()" in state["queries"][0]
    assert "status = 'downloading'" in state["queries"][0]


def test_nonterminal_transition_keeps_lease_and_terminal_transition_releases_it():
    queries = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, query, _parameters=()):
            queries.append(query)

        def fetchone(self):
            return {
                "instance_id": "gpu",
                "model_filename": "model.gguf",
                "config_version": 3,
            }

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def cursor(self):
            return Cursor()

    store = PostgresModelDownloadStore(
        "postgresql://test", connection_factory=lambda *_args, **_kwargs: Connection()
    )
    request_id = uuid4()
    lease_token = uuid4()

    store.mark_verifying(request_id, lease_token)
    verifying_query = queries[0]
    store.complete(request_id, lease_token)
    ready_query = queries[2]
    rollout_insert = queries[4]

    assert "lease_token = NULL" not in verifying_query
    assert "lease_until = NULL" not in verifying_query
    assert "lease_token = NULL" in ready_query
    assert "lease_until = NULL" in ready_query
    assert "version = %s AND model_filename = %s" in queries[3]
    assert (
        "INSERT INTO langgraph_service.inference_model_rollout_outbox" in rollout_insert
    )


def test_rollout_claim_is_version_qualified_and_uses_an_idempotent_lease():
    now = datetime.now(timezone.utc)
    event_id = uuid4()
    request_id = uuid4()
    lease_token = uuid4()
    state = {"queries": [], "fetches": 0}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, query, _parameters=()):
            state["queries"].append(query)

        def fetchone(self):
            state["fetches"] += 1
            if state["fetches"] == 1:
                return {
                    "event_id": event_id,
                    "instance_id": "cpu",
                    "download_request_id": request_id,
                    "model_filename": "cpu-model.gguf",
                    "config_version": 8,
                    "idempotency_key": f"cpu:8:{request_id}",
                }
            if state["fetches"] == 2:
                return {
                    "event_id": event_id,
                    "instance_id": "cpu",
                    "download_request_id": request_id,
                    "model_filename": "cpu-model.gguf",
                    "config_version": 8,
                    "idempotency_key": f"cpu:8:{request_id}",
                    "lease_token": lease_token,
                    "lease_until": now,
                    "attempts": 2,
                }
            return {"instance_id": "cpu"}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def cursor(self):
            return Cursor()

    store = PostgresModelDownloadStore(
        "postgresql://test", connection_factory=lambda *_args, **_kwargs: Connection()
    )
    claimed = store.claim_rollout(lease_seconds=120)

    assert claimed.instance_id == "cpu"
    assert claimed.download_request_id == request_id
    assert claimed.config_version == 8
    assert claimed.idempotency_key == f"cpu:8:{request_id}"
    assert claimed.lease_token == lease_token
    assert "config.version = event.config_version" in state["queries"][0]
    assert (
        "config.current_request_id = event.download_request_id" in state["queries"][1]
    )
    assert "FOR UPDATE OF event SKIP LOCKED" in state["queries"][1]
    assert "rollout_status = 'rolling_out'" in state["queries"][3]


def test_rollout_acknowledgment_replay_is_idempotent():
    event_id = uuid4()
    lease_token = uuid4()

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, *_args, **_kwargs):
            return None

        def fetchone(self):
            return {
                "event_id": event_id,
                "download_request_id": uuid4(),
                "instance_id": "gpu",
                "model_filename": "gpu-model.gguf",
                "config_version": 3,
                "status": "succeeded",
                "lease_token": lease_token,
                "lease_until": None,
                "lease_active": None,
            }

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def cursor(self):
            return Cursor()

    store = PostgresModelDownloadStore(
        "postgresql://test", connection_factory=lambda *_args, **_kwargs: Connection()
    )

    assert (
        store.acknowledge_rollout(event_id, lease_token, outcome="succeeded")
        == "succeeded"
    )


def test_rollout_claim_rejects_too_long_lease():
    store = PostgresModelDownloadStore("postgresql://unused")

    with pytest.raises(ValueError, match="between 30 and 900"):
        store.claim_rollout(lease_seconds=901)


def test_download_failure_clears_rollout_waiting_state_for_current_request():
    queries = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, query, _parameters=()):
            queries.append(query)

        def fetchone(self):
            return {"instance_id": "gpu"}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def cursor(self):
            return Cursor()

    store = PostgresModelDownloadStore(
        "postgresql://test", connection_factory=lambda *_args, **_kwargs: Connection()
    )
    store.fail(uuid4(), uuid4(), code="download_failed")

    assert "status IN ('downloading', 'verifying')" in queries[0]
    assert "rollout_status = 'not_requested'" in queries[1]
    assert "rollout_request_id = NULL" in queries[1]
