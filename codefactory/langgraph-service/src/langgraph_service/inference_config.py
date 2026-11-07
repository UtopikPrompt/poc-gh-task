"""Validated per-instance inference model configuration."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

InferenceInstance = Annotated[
    str,
    StringConstraints(
        pattern=r"^(?:gpu|cpu)(?:-[a-z0-9](?:[a-z0-9-]{0,57}[a-z0-9])?)?$",
    ),
]
DownloadStatus = Literal["queued", "downloading", "verifying", "ready", "failed"]
RolloutStatus = Literal["not_requested", "pending", "rolling_out", "ready", "failed"]
WorkloadStatus = Literal["provisioning", "ready", "deleting", "failed", "deleted"]


class InferenceInstanceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: InferenceInstance
    model_filename: str = Field(
        min_length=6,
        max_length=205,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.gguf$",
    )
    context_size: int = Field(default=1024, strict=True, ge=512, le=16384)
    threads: int = Field(default=2, strict=True, ge=1, le=8)
    rollout_status: RolloutStatus = "not_requested"
    rollout_request_id: str | None = None
    rollout_error: str | None = None
    workload_status: WorkloadStatus = "ready"
    workload_error: str | None = None

    @model_validator(mode="after")
    def validate_profile_limits(self) -> "InferenceInstanceConfig":
        maximum_context_size = 4096 if self.instance_id.startswith("cpu") else 16384
        if self.context_size > maximum_context_size:
            profile = "CPU" if self.instance_id.startswith("cpu") else "GPU"
            raise ValueError(f"context_size exceeds the {profile} instance profile limit")
        if self.instance_id.startswith("cpu") and self.threads > 4:
            raise ValueError("threads exceeds the CPU instance profile limit")
        return self
