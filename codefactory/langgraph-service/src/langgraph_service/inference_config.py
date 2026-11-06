"""Validated per-instance inference model configuration."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

InferenceInstance = Annotated[
    str,
    StringConstraints(
        pattern=r"^(?:gpu|cpu)(?:-[a-z0-9](?:[a-z0-9-]{0,57}[a-z0-9])?)?$",
    ),
]
DownloadStatus = Literal["queued", "downloading", "verifying", "ready", "failed"]
RolloutStatus = Literal["not_requested", "pending", "rolling_out", "ready", "failed"]


class InferenceInstanceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: InferenceInstance
    model_filename: str = Field(
        min_length=6,
        max_length=205,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.gguf$",
    )
    rollout_status: RolloutStatus = "not_requested"
    rollout_request_id: str | None = None
    rollout_error: str | None = None
