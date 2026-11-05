"""Validated declarative profiles for the fixed workflow stages."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

AgentRole = Literal["context", "planning", "risk", "implementation", "repair"]
ModelProfile = Literal["local_llama_cpp"]
ToolProfile = Literal[
    "gather_context",
    "create_plan",
    "assess_risk",
    "prepare_changes",
    "repair_changes",
]

ROLE_TOOL_PROFILES: dict[str, str] = {
    "context": "gather_context",
    "planning": "create_plan",
    "risk": "assess_risk",
    "implementation": "prepare_changes",
    "repair": "repair_changes",
}


class AgentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    prompt_content: str = Field(default="", max_length=4000)
    model_profile: ModelProfile
    skill_ids: list[str] = Field(default_factory=list, max_length=20)
    tool_profile: ToolProfile

    @model_validator(mode="after")
    def validate_skill_ids(self) -> AgentProfile:
        if any(
            not isinstance(skill_id, str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", skill_id)
            for skill_id in self.skill_ids
        ):
            raise ValueError("skill_ids must contain valid identifiers")
        if len(set(self.skill_ids)) != len(self.skill_ids):
            raise ValueError("skill_ids must be unique")
        return self


class AgentRoster(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    agents: list[AgentProfile] = Field(min_length=1, max_length=20)
    assignments: dict[str, str]

    @model_validator(mode="after")
    def validate_assignments(self) -> AgentRoster:
        roles = set(ROLE_TOOL_PROFILES)
        if set(self.assignments) != roles:
            raise ValueError("assignments must include every supported workflow role")
        agents_by_id = {agent.agent_id: agent for agent in self.agents}
        if len(agents_by_id) != len(self.agents):
            raise ValueError("agent_id values must be unique")
        for role, agent_id in self.assignments.items():
            profile = agents_by_id.get(agent_id)
            if profile is None:
                raise ValueError(f"assignment for {role} references an unknown agent")
            if profile.tool_profile != ROLE_TOOL_PROFILES[role]:
                raise ValueError(f"tool_profile is not allowed for the {role} role")
        return self

    def resolved_profiles(self) -> dict[str, dict[str, object]]:
        agents_by_id = {agent.agent_id: agent for agent in self.agents}
        return {
            role: agents_by_id[agent_id].model_dump(mode="json")
            for role, agent_id in self.assignments.items()
        }
