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
Docker builds remain optional. Both GHCR packages are public, so Kubernetes
pulls these images anonymously and no image pull secret is needed.

To publish `staging` or `prod`, open Actions > Build and publish service images
> Run workflow, select `main`, and choose the desired channel. Configure GitHub
Environments named `dev`, `staging`, and `prod`; set required reviewers and
restrict deployment branches to `main` for staging and prod so those publishing
jobs pause for environment approval. Manual dispatch from any other branch
validates but cannot publish.

The images include service dependencies and Uvicorn, and run as UID/GID 10001
with a read-only root filesystem.

## Operator-Provided Configuration

All three overlays include the same in-cluster PostgreSQL service, a persistent
StatefulSet, and a migration Job. No external database host or broker is
required. The platform base supplies `codefactory-database-config` defaults for
the internal hostname, port, database, service usernames, admin username, and
`sslmode=disable`. Database traffic is internal to the cluster; restrict access
to the PostgreSQL Service with the platform's network policy. The 10Gi
`ReadWriteOnce` claim uses the cluster's default StorageClass. PVCs survive pod
replacement, but backup, restore, and storage-class reclaim policy remain
operator responsibilities.

Create these namespaced secrets before deploying:

- Secret `codefactory-runtime-secrets`, with keys `POSTGRES_ADMIN_PASSWORD`,
  `GITHUB_DB_PASSWORD`, `LANGGRAPH_DB_PASSWORD`, `GITHUB_WEBHOOK_SECRET`, and
  `INTERNAL_SERVICE_TOKEN`. The migration Job uses the admin password and both
  service passwords; each running service receives only its own DB password.
- Secret `codefactory-github-app`, with key `private-key.pem`. The GitHub
  Deployment mounts this key read-only and sets `GITHUB_PRIVATE_KEY_PATH`.

ConfigMap `codefactory-github-settings` is included with empty values for
`GITHUB_APP_ID`, `GITHUB_PROJECT_OWNER`, `GITHUB_PROJECT_OWNER_TYPE`, and
`GITHUB_PROJECT_NUMBER`. Set the App ID, owner login, owner kind (`organization`
or `user`), and Project number before enabling GitHub delivery. The default
status field and option names are `Status`, `Ready`, and `Done`; optional
non-secret overrides are `GITHUB_PROJECT_STATUS_FIELD_NAME`,
`GITHUB_PROJECT_READY_OPTION_NAME`, and `GITHUB_PROJECT_DONE_OPTION_NAME`.
Project node, field, and option IDs are resolved through GraphQL and are never
operator-configured. Every signed webhook must include a positive installation
ID. The GitHub service stores it immutably with a new task and uses that same
installation for later PR, check, and completion calls; there is no static
installation setting or fallback. Existing tasks without an installation
context are not reused. No actual App, owner, or Project values are provided or
guessed.

The platform service ConfigMap sets `LLAMA_CPP_URL` to the internal
`http://llama-cpp-server.inference.svc.cluster.local:8080` service. The
LangGraph provider uses only the local llama.cpp `/completion` endpoint; do not
configure an external model provider. Install and verify the inference-platform
deployment and its pinned model artifacts, then confirm the model is loaded and
the internal service is ready before enabling Codefactory task intake. The
inference GPU deployment is intentionally not included in Codefactory
Kustomize overlays; deploy it through `inference-platform` and its own operator
runbook. `LLAMA_CPP_URL` may be overridden only for tests or non-Kubernetes
local development, and public hosts are rejected by the provider.

Before enabling source context and file proposals, the installed App needs
repository `Contents: Read` for bounded source reads and an effective
`Contents: Read and write` grant for task-branch proposals (which includes the
read access), `Pull requests: Read and write`, and `Issues: Read`, plus
`Projects: Read` for the configured Project owner and GitHub's baseline
repository metadata read. Verify these grants and selected-repository access on
the actual installation; the manifests do not create or assert them. Do not
grant a ruleset or branch-protection bypass. Proposal writes target only the
deterministic task branch and use non-force ref updates; the base branch is not
written.

The LangGraph image includes `langgraph_service.workflow_operations` as the
default factory, so no operator-supplied module name is needed. It calls the
internal llama.cpp provider and the task-scoped GitHub source-context API to
select candidate paths and retrieve a small set of bounded text files. It then
builds a structured plan, advisory risk summary, and typed proposal; CI repair
uses the recorded failures and the same task context. Risk output does not add
an approval gate. Proposal publication still uses the authenticated GitHub
API, which independently validates and writes only the deterministic task
branch. Tasks must remain disabled until inference artifacts are installed,
loaded, and ready.

The `codefactory-database-migrations` Job reads migration SQL from the service
images, applies the idempotent SQL migrations, then runs LangGraph's
`PostgresSaver.setup()` with the LangGraph login. Temporary schema-create access
is revoked before the Job exits. Both application Deployments wait for their
migration-created tables, so they cannot start against an uninitialized schema.
The Job runs once per created Job object. Before applying a release that changes
migrations, delete the prior completed Job and re-apply the environment overlay
so Kubernetes creates a fresh run; the runner and schema setup are repeat-safe.

Each service builds its PostgreSQL URL from the shared ConfigMap and its own
Secret-backed password. Complete database URLs are not stored in Secrets. The
environment database-config files retain the same explicit internal host and
SSL policy so a rendered overlay is self-contained.

### Local Dev Secrets

The dev-only files in
`infra-codefactory/kubernetes/environments/dev/secrets/` provide a local way to
create or update the two Secrets above. The tracked runtime `.env.example`
contains key names only; the private-key example documents the fixed local
filename. Local runtime credentials and `github-app.pem` are ignored by Git.
Keep the GitHub App private key separate from runtime and database credentials
in `runtime-secrets.env`.

From the repository root, create the runtime file from the example only when it
does not already exist. Create the private-key file at its fixed path:

```sh
runtime_env=infra-codefactory/kubernetes/environments/dev/secrets/runtime-secrets.env
if [[ ! -e "$runtime_env" ]]; then
  cp infra-codefactory/kubernetes/environments/dev/secrets/runtime-secrets.env.example "$runtime_env"
fi
```

Edit `runtime-secrets.env` in a local editor and fill the PostgreSQL admin
password, two application database passwords, and two runtime secret values.
The database endpoint, SSL mode, name, port, and usernames are supplied by the
platform base and environment overlay. Create
`infra-codefactory/kubernetes/environments/dev/secrets/github-app.pem` locally
and paste the GitHub App private key into it. Restrict each local file to the
current user with `chmod 600` after creating it. Do not put the private key in
an environment variable, shell command, or tracked file. The apply script checks
that required files and non-empty runtime keys exist before applying. For an
existing ignored `runtime-secrets.env`, do not overwrite it with the example.
It must contain only the listed service database passwords and runtime
secrets; non-secret database settings belong in the platform base or selected
environment patch. Ensure
namespace `codefactory` already exists; the script applies only the two Secrets.

Run the local apply mechanism from any directory:

```sh
bash infra-codefactory/kubernetes/environments/dev/secrets/apply-secrets.sh
```

It uses `kubectl create secret --dry-run=client` piped directly to
`kubectl apply`, so secret payloads are not placed in command-line arguments or
printed. To check resource names and key counts without exposing values:

```sh
kubectl -n codefactory get secret codefactory-runtime-secrets codefactory-github-app
```

Do not use `kubectl get secret -o yaml` or `-o json` when checking status. These
local files and instructions apply only to the dev overlay; staging and
production must receive equivalent secret resources through their approved
secret-management process.

## Webhook Ingress Prerequisite

Ingress and HTTPS termination are platform/environment responsibilities, not
external application services. No Ingress is included because a public
hostname, TLS secret/certificate policy, and Ingress class/controller were not
provided. Before enabling GitHub delivery, configure the platform-owned Ingress
to route `POST /webhooks/github` to Service `github-service` on port 8000, then
set the matching HTTPS webhook URL in the GitHub App. The service itself stays
ClusterIP-only.