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

Run the services, tests, and Kubernetes overlays from the repository root using
the commands in the root README and Makefile. Cross-service tests live in
`integration-tests/`; service-specific tests stay with their owning service.