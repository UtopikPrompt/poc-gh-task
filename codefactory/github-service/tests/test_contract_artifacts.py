import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator


CODEFACTORY_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_ROOT = CODEFACTORY_ROOT / "contracts"


def test_event_contracts_are_valid_json_schemas():
    schema_paths = sorted((CONTRACTS_ROOT / "events").rglob("*.json"))
    assert schema_paths

    for schema_path in schema_paths:
        schema = json.loads(schema_path.read_text())
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        examples = schema.get("examples", [])
        assert examples, f"{schema_path} must define at least one example"
        for example in examples:
            validator.validate(example)


def test_openapi_contracts_have_valid_metadata_and_local_references():
    openapi_paths = sorted((CONTRACTS_ROOT / "openapi").glob("*.json"))
    assert openapi_paths

    for openapi_path in openapi_paths:
        document = json.loads(openapi_path.read_text())
        assert document["openapi"].startswith("3.1.")
        assert document["info"]["title"]
        assert document["info"]["version"]
        assert document["paths"]

        for reference in _references(document):
            parsed_reference = urlsplit(reference)
            if parsed_reference.scheme or parsed_reference.netloc:
                continue
            if not parsed_reference.path:
                continue

            target = openapi_path.parent / unquote(parsed_reference.path)
            assert target.is_file(), f"{openapi_path}: unresolved $ref {reference}"


def test_workflow_inspection_contract_is_authenticated_and_read_only():
    github_contract = json.loads(
        (CONTRACTS_ROOT / "openapi" / "github-service.json").read_text()
    )
    operation = github_contract["paths"]["/internal/management/workflows"]["get"]
    assert operation["security"] == [{"bearerAuth": []}]
    assert not {
        "/api/workflows",
        "/api/agent-config",
        "/api/executions",
        "/api/executions/{thread_id}",
    } & set(github_contract["paths"])

    summary_schema = github_contract["components"]["schemas"]["WorkflowSummary"]
    Draft202012Validator.check_schema(summary_schema)
    Draft202012Validator(summary_schema).validate(
        {
            "task_id": "PVTI_item",
            "issue_number": None,
            "repository": "example/project",
            "pull_request_number": 47,
            "pull_request_id": "PR_node_id",
            "latest_event": "merged",
            "latest_event_at": "2026-11-02 12:00:00",
            "delivery_id": "delivery-3",
            "delivery_status": "accepted",
            "status": "queued",
            "attempts": 0,
            "has_failure": False,
            "pull_request_merged": True,
            "ci_status": "success",
            "ci_complete": True,
        }
    )

    langgraph_contract = json.loads(
        (CONTRACTS_ROOT / "openapi" / "langgraph-service.json").read_text()
    )
    description = langgraph_contract["info"]["description"].lower()
    assert "raw checkpoint blobs" in description
    assert "issue/source payloads" in description
    assert "no management api" not in description
    assert langgraph_contract["paths"]["/internal/management/agent-config"]["get"][
        "security"
    ] == [{"bearerAuth": []}]
    assert langgraph_contract["paths"]["/internal/management/executions"]["get"][
        "security"
    ] == [{"bearerAuth": []}]
    execution_schema = langgraph_contract["components"]["schemas"]["ExecutionSummary"]
    Draft202012Validator.check_schema(execution_schema)
    assert not {"issue", "context", "change", "checkpoint"} & set(
        execution_schema["properties"]
    )

    portal_contract = json.loads(
        (CONTRACTS_ROOT / "openapi" / "management-portal.json").read_text()
    )
    assert portal_contract["paths"]["/api/workflows"]["get"]["security"] == [
        {"portalSession": []}
    ]
    assert portal_contract["paths"]["/api/agent-config"]["put"]["security"] == [
        {"portalSession": []}
    ]
    assert portal_contract["paths"]["/api/agent-config"]["put"]["responses"]["403"]
    assert (
        portal_contract["components"]["securitySchemes"]["portalSession"]["name"]
        == "codefactory_dashboard_session"
    )
    config_schema = portal_contract["components"]["schemas"]["AgentConfig"]
    Draft202012Validator(config_schema).validate({"completion_timeout_seconds": 60})


def _references(value):
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str):
            yield reference
        for child in value.values():
            yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)
