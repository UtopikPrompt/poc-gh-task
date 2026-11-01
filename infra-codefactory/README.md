# Codefactory Services

This Kustomize unit deploys the GitHub webhook service and LangGraph worker as
separate workloads in the `codefactory` namespace. Render it locally with:

```sh
kubectl kustomize infra-codefactory/kubernetes/environments/dev
```

Optionally build the images locally from their service-local contexts:

```sh
docker build -t codefactory/github-service:dev codefactory/github-service
docker build -t codefactory/langgraph-service:dev codefactory/langgraph-service
```

The overlays use `ghcr.io/utopikprompt/poc-gh-task-github-service` and
`ghcr.io/utopikprompt/poc-gh-task-langgraph-service`, tagged `dev`, `staging`, or
`prod` according to the selected environment. GitHub Actions builds both images
without pushing on pull requests. A push to `main` runs validation first, then
publishes both images to GHCR with the full commit SHA and `dev` tags. The SHA
tags are not overwritten on workflow reruns; the channel tags point to the
selected commit. The workflow uses the repository-provided `GITHUB_TOKEN` and
does not need Docker, credentials, or a PAT on the developer's host. Local
Docker builds remain optional. Packages are not made public.

To publish `staging` or `prod`, open Actions > Build and publish service images
> Run workflow, select `main`, and choose the desired channel. Configure GitHub
Environments named `dev`, `staging`, and `prod`; set required reviewers and
restrict deployment branches to `main` for staging and prod so those publishing
jobs pause for environment approval. Manual dispatch from any other branch
validates but cannot publish.

Before deployment, create the operator-owned registry secret in namespace
`codefactory` (and create it separately in every namespace if that changes):

```sh
kubectl create secret docker-registry ghcr-pull-secret \
  --namespace codefactory \
  --docker-server=ghcr.io \
  --docker-username="$GHCR_USERNAME" \
  --docker-password="$GHCR_READ_PACKAGES_TOKEN"
```

Provide those environment variables securely from the operator's secret manager;
the credential must have read access to both GHCR packages. The Kustomize
Deployments reference this secret through `imagePullSecrets`. The images include
service dependencies and Uvicorn, and run as UID/GID 10001 with a read-only root
filesystem.

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

The ConfigMap `codefactory-service-config` in the shared Kubernetes base sets the internal
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