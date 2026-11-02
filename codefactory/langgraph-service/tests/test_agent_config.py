import pytest
from pydantic import ValidationError

from langgraph_service.agent_config import AgentConfig


def test_agent_config_defaults_to_supported_completion_timeout():
    assert AgentConfig().model_dump() == {"completion_timeout_seconds": 45}


@pytest.mark.parametrize("value", [1, 90])
def test_agent_config_accepts_existing_timeout_bounds(value):
    assert (
        AgentConfig(completion_timeout_seconds=value).completion_timeout_seconds
        == value
    )


@pytest.mark.parametrize("value", [0, 91, True, "45"])
def test_agent_config_rejects_invalid_timeout_values(value):
    with pytest.raises(ValidationError):
        AgentConfig(completion_timeout_seconds=value)
