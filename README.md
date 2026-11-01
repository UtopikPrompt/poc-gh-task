# Codefactory Monorepo
LangGraph multi-repo task platform on Kubernetes

## Services

This repository contains three independently owned projects. `codefactory/` contains the GitHub and LangGraph services, their API/event contracts, integration tests, and architecture documentation. `infra-codefactory/` contains Kubernetes bases and environment overlays for those services. `inference-platform/` owns model-serving code, inference Kubernetes resources, and inference documentation.

The GitHub service verifies public webhooks, normalizes events, owns the durable event ledger and queue, provides bounded GitHub App capabilities, and is the only service that writes Project status. The LangGraph service claims durable events, owns graph execution/checkpoints, and requests PR publication or completion through the authenticated GitHub-service API. They communicate through authenticated HTTP; there is no shared Python implementation, Redis, or Kafka dependency.

Production uses PostgreSQL. GitHub ledger/queue tables live in the `github_service` schema; LangGraph checkpoint tables live in `langgraph_service`. Apply `codefactory/github-service/migrations/001_github_event_queue.sql`, then `codefactory/langgraph-service/migrations/001_checkpoint_schema.sql`, then `codefactory/github-service/migrations/002_service_roles.sql` with an operator-controlled database administration identity. The runtime services use separate PostgreSQL URLs and restricted database roles. Configure login roles, grants, network policy, TLS, and secret delivery for your environment; no database password or service token is embedded here.

Run the services independently with Uvicorn after configuring their environment:

```sh
PYTHONPATH=codefactory/github-service/src uvicorn github_service.host:create_app --factory --host 0.0.0.0 --port 8000
PYTHONPATH=codefactory/langgraph-service/src uvicorn langgraph_service.host:create_app --factory --host 0.0.0.0 --port 8001
```

Optionally build service images locally with each service as the build context:

```sh
docker build -t codefactory/github-service:dev codefactory/github-service
docker build -t codefactory/langgraph-service:dev codefactory/langgraph-service
```

Local Docker is not required for validation or deployment image publishing. The
GitHub Actions workflow builds both images for pull requests without pushing.
On pushes to `main`, it validates the repository and then publishes to GHCR;
manual runs from `main` can publish the `dev`, `staging`, or `prod` channel.
Each publish uses the lowercase packages
`ghcr.io/utopikprompt/poc-gh-task-github-service` and
`ghcr.io/utopikprompt/poc-gh-task-langgraph-service`, tagged with the full commit
SHA and the selected channel. The SHA tag is retained on reruns and channel tags
are moved to the selected commit.

Configure the GitHub Actions environments named `dev`, `staging`, and `prod`.
Set required reviewers and restrict deployment branches to `main` for staging
and production as appropriate. To publish a staging or production build, use
Actions > Build and publish service images > Run workflow, select branch `main`,
and choose the channel; the workflow waits for any configured environment
approval. Registry packages remain private and the workflow uses its
`GITHUB_TOKEN`, not a repository PAT.

Before deploying, create the namespaced `ghcr-pull-secret` in `codefactory` as
described in [infra-codefactory/README.md](infra-codefactory/README.md). The
cluster needs a GHCR credential with package read access; no credential values
are stored here.

The LangGraph service requires an operator-owned `WORKFLOW_OPERATIONS_MODULE` exporting `make_operations() -> WorkflowOperations`. The injected operations perform bounded context gathering, planning, risk assessment, change preparation, and optional repair. `prepare_changes` returns `pull_request_title`, `pull_request_body`, `head_branch`, and `base_branch`; PR creation itself is a GitHub-service capability scoped to the repository resolved from the Project event. Plugins must be safe to retry and must not invoke shell/process commands, merge PRs, or write target branches.

The LangGraph image does not include that operator-owned module. Make it available as an importable module at build time or runtime, for example in an operator-built image or on a runtime-mounted Python module path. Supply credentials and other secrets through runtime secret management, not image build arguments or image contents.

GitHub-native repository rules must require all applicable PR checks before a human can merge. Following a merge webhook, GitHub service fetches the current PR identity/state and current check results again; it updates the mapped Project task to `done` only when the matching PR is merged and all reported checks are successful. LangGraph has no Project-write or merge capability.

## Local Development And Tests

Install the independently declared service test dependencies in a virtual environment, then run the complete suite and checks:

```sh
python3 -m venv .venv
. .venv/bin/activate
make install-dev
make check
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

- Design document: [codefactory/docs/langgraph-kubernetes-github-project-issue.md](codefactory/docs/langgraph-kubernetes-github-project-issue.md)
- Inference project: [inference-platform/README.md](inference-platform/README.md)
- Service deployment overlays: [infra-codefactory/kubernetes/environments](infra-codefactory/kubernetes/environments)
- This repository captures the architecture for a multi-repository workflow platform built around LangGraph orchestration, bounded specialized workers, model-role routing, and Kubernetes-based execution isolation.
