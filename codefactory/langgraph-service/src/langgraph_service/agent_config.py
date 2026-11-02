"""Validated editable settings for the existing agent runtime."""

from pydantic import BaseModel, ConfigDict, Field


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    completion_timeout_seconds: int = Field(default=45, strict=True, ge=1, le=90)
