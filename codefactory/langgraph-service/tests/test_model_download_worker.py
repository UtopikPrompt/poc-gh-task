from datetime import datetime, timezone
from uuid import uuid4

from langgraph_service.model_download import ModelDownloadError
from langgraph_service.model_download_worker import process_one


def _request():
    now = datetime.now(timezone.utc)
    return type(
        "ClaimedRequest",
        (),
        {
            "request_id": uuid4(),
            "lease_token": uuid4(),
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
        },
    )()


class FakeStore:
    def __init__(self):
        self.request = _request()
        self.transitions = []

    def claim(self, *, lease_seconds):
        assert lease_seconds == 120
        return self.request

    def mark_verifying(self, request_id, lease_token):
        assert (request_id, lease_token) == (
            self.request.request_id,
            self.request.lease_token,
        )
        self.transitions.append("verifying")

    def complete(self, request_id, lease_token):
        assert (request_id, lease_token) == (
            self.request.request_id,
            self.request.lease_token,
        )
        self.transitions.append("ready")

    def fail(self, request_id, lease_token, *, code):
        assert (request_id, lease_token) == (
            self.request.request_id,
            self.request.lease_token,
        )
        self.transitions.append(("failed", code))

    def renew(self, *_args, **_kwargs):
        return True


def test_worker_completes_only_after_verification_callback(tmp_path):
    store = FakeStore()

    def downloader(**kwargs):
        kwargs["on_progress"]()
        kwargs["on_verifying"]()

    processed = process_one(store, model_directory=tmp_path, downloader=downloader)

    assert processed is True
    assert store.transitions == ["verifying", "ready"]


def test_worker_persists_fixed_failure_code_not_exception_text(tmp_path):
    store = FakeStore()

    def downloader(**_kwargs):
        raise ModelDownloadError("transport failed with token=credential-marker")

    process_one(store, model_directory=tmp_path, downloader=downloader)

    assert store.transitions == [("failed", "download_failed")]
    assert "credential-marker" not in repr(store.transitions)
