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