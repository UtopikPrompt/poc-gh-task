# Codefactory Services

This Kustomize unit deploys the GitHub webhook service and LangGraph worker as
separate workloads in the `codefactory` namespace. Render it locally with:

```sh
kubectl kustomize deploy/kubernetes/codefactory
```

Build the images from their service-local contexts:

```sh
docker build -t codefactory/github-service:dev services/github-service
docker build -t codefactory/langgraph-service:dev services/langgraph-service
```

The workloads use the fixed image references `codefactory/github-service:dev`
and `codefactory/langgraph-service:dev`. Those development images are not
published; make them available to the cluster before any rollout. The images
include service dependencies and Uvicorn, and run as UID/GID 10001 with a
read-only root filesystem.

## Operator-Provided Configuration

Create these namespaced resources before deploying; this directory intentionally
does not contain credential values or substitutes for operator-owned settings:

- Secret `codefactory-runtime-secrets`, with keys `GITHUB_DATABASE_URL`,
  `LANGGRAPH_DATABASE_URL`, `GITHUB_WEBHOOK_SECRET`, and
  `INTERNAL_SERVICE_TOKEN`. The two database URLs are separate credentials for
  the externally provisioned PostgreSQL service.
- Secret `codefactory-github-app`, with key `private-key.pem`. The GitHub
  Deployment mounts this key as a read-only file and sets
  `GITHUB_PRIVATE_KEY_PATH` to its path.
- ConfigMap `codefactory-github-settings`, with keys `GITHUB_APP_ID`,
  `GITHUB_INSTALLATION_ID`, `GITHUB_PROJECT_NODE_ID`,
  `GITHUB_PROJECT_STATUS_FIELD_ID`, `GITHUB_PROJECT_READY_OPTION_ID`, and
  `GITHUB_PROJECT_DONE_OPTION_ID`.
- ConfigMap `codefactory-langgraph-settings`, with key
  `WORKFLOW_OPERATIONS_MODULE`. Supply the real importable module name that
  exports `make_operations() -> WorkflowOperations`; this repository does not
  provide or assume that module.

The ConfigMap `codefactory-service-config` in this directory sets the internal
GitHub service URL and worker polling interval. The PostgreSQL databases, schema
migrations, roles, network access, and secret delivery are managed separately.
Apply the GitHub event-queue migration, LangGraph checkpoint migration, and
GitHub service-role migration in the order documented in the project README.
No database Deployment or PVC is included.

## Webhook Ingress Prerequisite

No Ingress is included because a public hostname, TLS secret/certificate policy,
and Ingress class/controller are environment-specific and have not been
provided. Before enabling GitHub delivery, configure an Ingress owned by the
environment to route `POST /webhooks/github` to Service `github-service` on port
8000. Select its host, TLS configuration, and class explicitly, then set the
matching HTTPS webhook URL in the GitHub App. The service itself remains
ClusterIP-only.