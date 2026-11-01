# poc-gh-task
LangGraph multi-repo task platform on Kubernetes

## Services

`codefactory.github_service` and `codefactory.langgraph_service` are independently runnable services. The GitHub service verifies public webhooks, normalizes events, owns the durable event ledger and queue, provides bounded GitHub App capabilities, and is the only service that writes Project status. The LangGraph service claims durable events, owns graph execution/checkpoints, and requests PR publication or completion through the authenticated GitHub-service API. There is no Redis or Kafka dependency.

Production uses PostgreSQL. GitHub ledger/queue tables live in the `github_service` schema; LangGraph checkpoint tables live in `langgraph_service`. Apply `github_service/migrations/001_github_event_queue.sql`, then `langgraph_service/migrations/001_checkpoint_schema.sql`, then `github_service/migrations/002_service_roles.sql` with an operator-controlled database administration identity. The runtime services use separate PostgreSQL URLs and restricted database roles. Configure login roles, grants, network policy, TLS, and secret delivery for your environment; no database password or service token is embedded here.

Run the services independently with Uvicorn after configuring their environment:

```sh
uvicorn codefactory.github_service.host:create_app --factory --host 0.0.0.0 --port 8000
uvicorn codefactory.langgraph_service.host:create_app --factory --host 0.0.0.0 --port 8001
```

The LangGraph service requires an operator-owned `WORKFLOW_OPERATIONS_MODULE` exporting `make_operations() -> WorkflowOperations`. The injected operations perform bounded context gathering, planning, risk assessment, change preparation, and optional repair. `prepare_changes` returns `pull_request_title`, `pull_request_body`, `head_branch`, and `base_branch`; PR creation itself is a GitHub-service capability scoped to the repository resolved from the Project event. Plugins must be safe to retry and must not invoke shell/process commands, merge PRs, or write target branches.

GitHub-native repository rules must require all applicable PR checks before a human can merge. Following a merge webhook, GitHub service fetches the current PR identity/state and current check results again; it updates the mapped Project task to `done` only when the matching PR is merged and all reported checks are successful. LangGraph has no Project-write or merge capability.

## Local Development And Tests

Install the runtime and local test dependencies in a virtual environment, then run the suite:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

Tests use temporary SQLite files, mocked PostgreSQL connections, and GitHub API fixtures. They cover delivery idempotency, queue leasing/retries, authenticated service contracts, check gating, PR publication scoping, and the completion boundary. They make no live GitHub, PostgreSQL, or cluster requests. SQLite is for local development/tests only; production hosts require the PostgreSQL DSNs.

Configure these values through the runtime's secret manager or environment; do not commit credentials:

| Variable | Purpose |
| --- | --- |
| `WORKFLOW_OPERATIONS_MODULE` | LangGraph service: importable module exporting `make_operations() -> WorkflowOperations` |
| `GITHUB_WEBHOOK_SECRET` | Secret used to verify webhook signatures |
| `GITHUB_DATABASE_URL` | GitHub service PostgreSQL connection for ledger, queue, mappings, and task context |
| `LANGGRAPH_DATABASE_URL` | LangGraph service PostgreSQL connection for checkpoints |
| `INTERNAL_SERVICE_TOKEN` | Shared bearer token for the private LangGraph-to-GitHub API |
| `GITHUB_SERVICE_URL` | Internal GitHub service base URL; defaults to `http://github-service:8000` |
| `GITHUB_APP_ID`, `GITHUB_INSTALLATION_ID` | GitHub App and installation identifiers |
| `GITHUB_PRIVATE_KEY_PATH` | Path to the App private key file |
| `GITHUB_API_URL` | Optional API origin; defaults to `https://api.github.com` |
| `GITHUB_PROJECT_NODE_ID` | Organization Project node ID |
| `GITHUB_PROJECT_STATUS_FIELD_ID` | Project single-select status field ID |
| `GITHUB_PROJECT_READY_OPTION_ID` | Status option that starts work |
| `GITHUB_PROJECT_DONE_OPTION_ID` | Status option set after successful merge |
| `LOCAL_SQLITE_PATH` | Local-only SQLite path; defaults to `./var/github-local.sqlite` |
| `WEBHOOK_MAX_BYTES` | Request size limit; defaults to 1,000,000 and must not exceed 10,000,000 |
| `WORKER_POLL_SECONDS`, `WORKER_LEASE_SECONDS`, `WORKER_MAX_ATTEMPTS` | LangGraph queue polling, lease, and bounded retry settings |

Subscribe the GitHub App to organization `projects_v2_item` deliveries and repository `check_run` and `pull_request` deliveries. Configure a dedicated test Project, actual least-privilege App grants, protected-branch required checks, database roles/DSNs, internal authentication and network policy, and a reviewed operations plugin before any live run. These are production prerequisites, not values or validation results supplied by this repository.

## Architecture notes

- Design document: [docs/langgraph-kubernetes-github-project-issue.md](docs/langgraph-kubernetes-github-project-issue.md)
- This repository captures the architecture for a multi-repository workflow platform built around LangGraph orchestration, bounded specialized workers, model-role routing, and Kubernetes-based execution isolation.
