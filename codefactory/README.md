# Codefactory

Codefactory contains three independently installed and built services. Each
service declares its runtime and test dependencies in its own `pyproject.toml`,
uses a `src/` package layout, and is the root of its own Docker build context.
There is no shared Python package. The LangGraph service calls GitHub-owned
capabilities over authenticated HTTP; PostgreSQL inbox/outbox and checkpoints
are the initial durable stores. The management portal calls GitHub and LangGraph
through their authenticated service APIs and has no database access.

The event JSON Schemas are in [`contracts/events/v1`](contracts/events/v1), and
the service OpenAPI documents are in [`contracts/openapi`](contracts/openapi).
The normalized `issue` object remains open because the upstream GitHub fields
returned by the GraphQL query are not a stable platform-owned schema. API
responses returned by GitHub callbacks are also open where their exact provider
payload is not controlled here.

The GitHub Service OpenAPI contract includes authenticated bounded source
context and file-proposal capabilities. GitHub Service resolves repository,
installation, and default branch only from the GitHub-owned task record; it
returns safe candidate paths and selected text files, then writes proposals
only to the deterministic task branch and creates or reuses the verified PR.
The bundled LangGraph operations use the internal llama.cpp `/completion`
service for path selection, planning, advisory risk summaries, and typed
changes. The provider is local-only and fails closed on invalid or truncated
output. GitHub Service independently validates source paths and proposals.

Per-instance inference artifact requests, the durable downloader worker contract,
and Prometheus-backed CPU/GPU metrics are documented in
[`docs/inference-model-download-contract.md`](docs/inference-model-download-contract.md).

Run the services, tests, and Kubernetes overlays from the repository root using
the commands in the root README and Makefile. Cross-service tests live in
`integration-tests/`; service-specific tests stay with their owning service.

## Management portal

The independently built Management Portal serves `/` through its own
`management-portal` ClusterIP. It calls GitHub Service for safe task-event
summaries and LangGraph Service for execution projections and agent
configuration; it has no database access. Signed GitHub OAuth sessions can
inspect those summaries. Only sessions whose GitHub Organization Owner role was
verified may change the local-model completion timeout. The setting is
versioned in the LangGraph database and captured when a new workflow thread
starts; in-flight runs keep their checkpointed setting. Neither secrets nor raw
checkpoint, issue, or source payloads are exposed. The public root and auth/API
paths route to the portal, while `/webhooks/github` remains routed to GitHub
Service. To view it locally in the `codefactory` namespace, run:

```sh
kubectl -n codefactory port-forward service/management-portal 8000:8000
```

Then open <http://127.0.0.1:8000/>. The summary includes task, repository,
pull-request, latest event, queue status, and timestamps. Issue numbers are
shown as unavailable because the current schema only stores them in private
event payloads, which the dashboard does not query. Failure details are
withheld; only failure presence and status are shown. If the summary store is
unavailable, the page displays an unavailable state.