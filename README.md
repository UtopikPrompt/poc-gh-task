# Codefactory Monorepo
LangGraph multi-repo task platform on Kubernetes

## Services

This repository contains three independently built Codefactory services and the independently owned inference project. `codefactory/` contains the GitHub, LangGraph, and management-portal services, their API/event contracts, integration tests, and architecture documentation. Each service has its own dependencies, tests, and Docker build context. `infra-codefactory/` contains Kubernetes bases and environment overlays for those services. `inference-platform/` owns model-serving code, inference Kubernetes resources, and inference documentation.

The GitHub service verifies public webhooks, normalizes events, owns the durable event ledger and queue, provides bounded GitHub App capabilities, and is the only service that writes Project status. The LangGraph service claims durable events, owns graph execution/checkpoints, and requests bounded file-change proposal application or task completion through the authenticated GitHub-service API. GitHub Service alone owns repository identity, GitHub App calls, branch writes, and PR creation/update. They communicate through authenticated HTTP; there is no shared Python implementation, Redis, or Kafka dependency.

The management portal owns browser authentication, workflow inspection, and
the agent configuration UI. It has no database access: safe workflow summaries
come from GitHub Service, and execution/configuration data comes from LangGraph
Service. GitHub OAuth verifies active membership in `UtopikPrompt`; only a
verified Organization Owner may edit the non-secret configuration.

All three Kustomize overlays include the same internal PostgreSQL StatefulSet with a 10Gi `ReadWriteOnce` PVC using the default StorageClass. Shared ConfigMap defaults set the database name, host, port, usernames, admin username, and `sslmode=disable`; no external database or broker is required. The GitHub and LangGraph schemas retain separate `NOLOGIN` runtime roles. Restrict PostgreSQL access with the platform's network policy; PVC backup, restore, and reclaim policy remain operator responsibilities.

The `codefactory-database-migrations` Job reads SQL migrations from the owning service images, applies the idempotent migrations, and runs LangGraph `PostgresSaver.setup()` using the LangGraph login. Temporary schema-create access is revoked before the Job exits. Both application Deployments wait for their migration-created tables before startup. Runtime services no longer run checkpoint setup. Recreate the completed Job before applying a release that introduces migrations. Database passwords are delivered through Secret keys; complete URLs are constructed at runtime. The repository does not run migrations against a live database during validation.

Run the services independently with Uvicorn after configuring their environment:

```sh
PYTHONPATH=codefactory/github-service/src uvicorn github_service.host:create_app --factory --host 0.0.0.0 --port 8000
PYTHONPATH=codefactory/langgraph-service/src uvicorn langgraph_service.host:create_app --factory --host 0.0.0.0 --port 8001
PYTHONPATH=codefactory/management-portal/src uvicorn management_portal.host:create_app --factory --host 0.0.0.0 --port 8002
```

For UI work, run the loopback-only preview app. It serves working-tree assets
without OAuth and reads live workflow data from the dev services. Start these
port-forwards in separate terminals:

```sh
kubectl -n codefactory port-forward --address 127.0.0.1 service/github-service 18000:8000
kubectl -n codefactory port-forward --address 127.0.0.1 service/langgraph-service 18001:8001
```

Install the portal dependencies in a local virtual environment once:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e codefactory/management-portal
```

Then start the preview. It reads the internal service token from the dev Secret
into the process environment; do not print or commit the token. The preview is
read-only and binds only to localhost:

```sh
GITHUB_SERVICE_URL=http://127.0.0.1:18000 \
LANGGRAPH_SERVICE_URL=http://127.0.0.1:18001 \
INTERNAL_SERVICE_TOKEN="$(kubectl -n codefactory get secret codefactory-runtime-secrets -o jsonpath='{.data.INTERNAL_SERVICE_TOKEN}' | base64 --decode)" \
PYTHONPATH=codefactory/management-portal/src \
	python -m uvicorn \
	management_portal.preview:create_app --factory --reload \
	--reload-dir codefactory/management-portal/src \
	--host 127.0.0.1 --port 8002
```

Optionally build service images locally with each service as the build context:

```sh
docker build -t codefactory/github-service:dev codefactory/github-service
docker build -t codefactory/langgraph-service:dev codefactory/langgraph-service
docker build -t codefactory/management-portal:dev codefactory/management-portal
```

Local Docker is not required for validation or deployment image publishing. The
GitHub Actions workflow builds all three service images for pull requests without pushing.
On pushes to `main`, it validates the repository and then publishes to GHCR;
manual runs from `main` can publish the `dev`, `staging`, or `prod` channel.
Each publish uses the lowercase packages
`ghcr.io/utopikprompt/poc-gh-task-github-service` and
`ghcr.io/utopikprompt/poc-gh-task-langgraph-service`, and
`ghcr.io/utopikprompt/poc-gh-task-management-portal`, tagged with the full commit
SHA and the selected channel. The SHA tag is retained on reruns and channel tags
are moved to the selected commit.

Configure the GitHub Actions environments named `dev`, `staging`, and `prod`.
Set required reviewers and restrict deployment branches to `main` for staging
and production as appropriate. To publish a staging or production build, use
Actions > Build and publish service images > Run workflow, select branch `main`,
and choose the channel; the workflow waits for any configured environment
approval. Registry packages are public, so Kubernetes pulls them anonymously
without an image pull secret. The publishing workflow uses its `GITHUB_TOKEN`,
not a repository PAT.

The intended human gates are: set the Project task to the configured start status (default `Ready`), then validate the PR by reviewing and merging it only after required CI checks succeed. The proposal API is implemented, but the bundled production operations factory is empty and no provider generates a proposal; ready tasks stop at context and no PR is produced. If an explicitly configured provider submits changes, GitHub Service validates and applies them to a deterministic task branch, then creates or reuses the verified task PR. Phase 2 `/agent <prompt>` rework may reuse this same-PR path, but its command, authorization, and clarification semantics remain unimplemented.

The LangGraph image bundles `langgraph_service.workflow.workflow_operations` as its default factory, so no external module path is required. The factory intentionally returns empty operations: a ready task stops at `pending_external_operation` during context gathering and no proposal or PR is produced. A configured `prepare_changes` operation may return only a `changes` array of create/update file changes; the authenticated proposal API bounds it to 20 files, 64 KiB per file, 256 KiB total, and 2 MiB per raw request. GitHub Service obtains the repository and default branch from GitHub-owned task/repository state, writes only the deterministic task branch using non-force ref updates, and verifies/reuses the task PR. No context, planning, risk, change-preparation, or repair provider is included. Supply credentials and other secrets through runtime secret management, not image build arguments or image contents.

GitHub-native repository rules must require all applicable PR checks before a human can merge. Following a merge webhook, GitHub service fetches the current PR identity/state and current check results again; it updates the mapped Project task to `done` only when the matching PR is merged and all reported checks are successful. LangGraph has no Project-write or merge capability.

## Local Development And Tests

Install the independently declared service test dependencies in a virtual environment, then run the complete suite and checks:

```sh
python3 -m venv .venv
. .venv/bin/activate
make install-dev
make check
```

Tests use temporary SQLite files, mocked PostgreSQL connections, and GitHub API fixtures. They cover delivery idempotency, queue leasing/retries, authenticated service contracts, bounded proposal validation and branch/PR reuse, check gating, and the completion boundary. They make no live GitHub, PostgreSQL, or cluster requests. SQLite is for local development/tests only; production hosts require the PostgreSQL DSNs.

Configure endpoint settings through the environment's Kubernetes ConfigMap and credentials through its runtime Secret; do not commit credentials:

| Variable | Purpose |
| --- | --- |
| `WORKFLOW_OPERATIONS_MODULE` | Optional LangGraph module override; default is the bundled safe factory, which leaves task operations pending |
| `GITHUB_WEBHOOK_SECRET` | Secret used to verify webhook signatures |
| `POSTGRES_ADMIN_USERNAME` | Shared ConfigMap default: `codefactory_admin` |
| `POSTGRES_ADMIN_PASSWORD` | PostgreSQL admin password in the runtime Secret; used by initialization and the migration Job |
| `GITHUB_DB_HOST`, `GITHUB_DB_PORT`, `GITHUB_DB_NAME`, `GITHUB_DB_USERNAME`, `GITHUB_DB_SSLMODE` | Shared internal ConfigMap defaults: `codefactory-postgres`, `5432`, `codefactory`, `codefactory_github`, and `disable` |
| `GITHUB_DB_PASSWORD` | GitHub service database password in its runtime Secret |
| `LANGGRAPH_DB_HOST`, `LANGGRAPH_DB_PORT`, `LANGGRAPH_DB_NAME`, `LANGGRAPH_DB_USERNAME`, `LANGGRAPH_DB_SSLMODE` | Shared internal ConfigMap defaults: `codefactory-postgres`, `5432`, `codefactory`, `codefactory_langgraph`, and `disable` |
| `LANGGRAPH_DB_PASSWORD` | LangGraph service database password in its runtime Secret |
| `INTERNAL_SERVICE_TOKEN` | Shared bearer token for private portal-to-service and LangGraph-to-GitHub APIs |
| `GITHUB_SERVICE_URL` | Internal GitHub service base URL; defaults to `http://github-service:8000` |
| `LANGGRAPH_SERVICE_URL` | Internal LangGraph service base URL; defaults to `http://langgraph-service:8001` |
| `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET` | GitHub OAuth App credentials used by the management portal |
| `DASHBOARD_SESSION_SIGNING_KEY` | Portal session-signing key; configure at least 32 random bytes |
| `GITHUB_APP_ID` | GitHub App ID |
| `GITHUB_PRIVATE_KEY_PATH` | Path to the App private key file |
| `GITHUB_API_URL` | Optional API origin; defaults to `https://api.github.com` |
| `GITHUB_PROJECT_STATUS_FIELD_NAME` | Optional status field name; defaults to `Status` |
| `GITHUB_PROJECT_READY_OPTION_NAME` | Optional ready option name; defaults to `Ready` |
| `GITHUB_PROJECT_IN_PROGRESS_OPTION_NAME` | Optional execution option name; defaults to `In Progress` |
| `GITHUB_PROJECT_DONE_OPTION_NAME` | Optional completion option name; defaults to `Done` |
| `LOCAL_SQLITE_PATH` | Local-only SQLite path; defaults to `./var/github-local.sqlite` |
| `WEBHOOK_MAX_BYTES` | Request size limit; defaults to 1,000,000 and must not exceed 10,000,000 |
| `WORKER_POLL_SECONDS`, `WORKER_LEASE_SECONDS`, `WORKER_MAX_ATTEMPTS` | LangGraph queue polling, lease, and bounded retry settings |

Each `projects_v2_item` delivery is resolved by item node ID through GraphQL using that event's installation. Project ID, owner login/type, status field/options, linked issue, and repository are derived from GitHub; the repository is independently checked with the same installation's REST token. An item starts work only when its Status is `Ready` and its issue repository is accessible to that installation. Projects with organization or user owners are supported when the installation can read the Project item; inaccessible Project data or repositories fail closed. No global Project owner/number, installation ID, or opaque Project/field/option IDs are configured. The Project identity, issue repository, and installation are persisted immutably per task, and completion revalidates the same item before setting its Status to `Done`. Subscribe the GitHub App to organization `projects_v2_item` deliveries and repository `check_run` and `pull_request` deliveries. Planned Phase 2 additionally requires repository `issue_comment` events for `/agent <prompt>` on PRs and replies to pending clarification questions on linked issues; ordinary comments must not trigger work. Configure actual least-privilege App grants, protected-branch required checks, runtime secrets, internal authentication, and network policy before any live run. The platform Ingress routes `/` and portal authentication/API paths to Management Portal, and the exact `POST /webhooks/github` path to GitHub Service. Configure its hostname, certificate/secret policy, and Ingress class, then set the matching GitHub App webhook URL. Those ingress values are platform responsibilities, not application external services. The bounded operations implementation remains a code prerequisite, not an operator-supplied missing module.

Proposal deployment prerequisite: the GitHub App needs repository `Contents: Read and write`, `Pull requests: Read and write`, and `Issues: Read`, plus `Projects: Read` and access to each linked Project owner; repository metadata read is also required (GitHub's baseline permission). Every linked issue repository is checked against the event installation before intake, and GraphQL Project resolution must succeed under that installation. These grants and selected-repository access must be verified on the actual installation. The repository does not configure or assert installed grants and must not grant the App a branch-protection/ruleset bypass.

## Architecture notes

- Design document: [codefactory/docs/langgraph-kubernetes-github-project-issue.md](codefactory/docs/langgraph-kubernetes-github-project-issue.md)
- Inference project: [inference-platform/README.md](inference-platform/README.md)
- Service deployment overlays: [infra-codefactory/kubernetes/environments](infra-codefactory/kubernetes/environments)
- This repository captures the architecture for a multi-repository workflow platform built around LangGraph orchestration, bounded specialized workers, model-role routing, and Kubernetes-based execution isolation.
