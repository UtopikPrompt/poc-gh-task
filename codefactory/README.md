# Codefactory

Codefactory contains two independently installed and built services. Each
service declares its runtime and test dependencies in its own `pyproject.toml`,
uses a `src/` package layout, and is the root of its own Docker build context.
There is no shared Python package. The LangGraph service calls GitHub-owned
capabilities over authenticated HTTP; PostgreSQL inbox/outbox and checkpoints
are the initial durable stores.

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

Run the services, tests, and Kubernetes overlays from the repository root using
the commands in the root README and Makefile. Cross-service tests live in
`integration-tests/`; service-specific tests stay with their owning service.

## Workflow state dashboard

The GitHub Service serves a read-only workflow dashboard at `/` and its safe
summary API at `/api/workflows`. Both are reachable through the existing
`github-service` ClusterIP; no public route is added. To view it locally in the
`codefactory` namespace, run:

```sh
kubectl -n codefactory port-forward service/github-service 8000:8000
```

Then open <http://127.0.0.1:8000/>. The summary includes task, repository,
pull-request, latest event, queue status, and timestamps. Issue numbers are
shown as unavailable because the current schema only stores them in private
event payloads, which the dashboard does not query. Failure details are
withheld; only failure presence and status are shown. If the summary store is
unavailable, the page displays an unavailable state.