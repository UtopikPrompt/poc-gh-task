"""Validated per-instance inference model configuration."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

InferenceInstance = Literal["gpu", "cpu"]
DownloadStatus = Literal["queued", "downloading", "verifying", "ready", "failed"]
RolloutStatus = Literal[
    "not_requested", "waiting_for_download", "pending", "rolling_out", "ready", "failed"
]


class InferenceInstanceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: InferenceInstance
    model_filename: str = Field(
        min_length=6,
        max_length=205,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.gguf$",
    )
    model_url: str | None = None
    model_sha256: str | None = Field(default=None, pattern=r"^[A-Fa-f0-9]{64}$")
    download_status: DownloadStatus = "ready"
    download_request_id: str | None = None
    download_error: str | None = None
    rollout_status: RolloutStatus = "not_requested"
    rollout_request_id: str | None = None
    rollout_error: str | None = None
