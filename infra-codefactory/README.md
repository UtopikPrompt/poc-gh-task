# Codefactory Services

This Kustomize unit deploys the GitHub webhook service, LangGraph worker, and
management portal as separate workloads in the `codefactory` namespace. Render
it locally with:

```sh
kubectl kustomize infra-codefactory/kubernetes/environments/dev
```

Optionally build the images locally from their service-local contexts:

```sh
docker build -t codefactory/github-service:dev codefactory/github-service
docker build -t codefactory/langgraph-service:dev codefactory/langgraph-service
docker build -t codefactory/management-portal:dev codefactory/management-portal
```

The overlays use the `poc-gh-task-github-service`,
`poc-gh-task-langgraph-service`, and `poc-gh-task-management-portal` GHCR images,
tagged `dev`, `staging`, or `prod` according to the selected environment.
GitHub Actions builds all three images without pushing on pull requests. A push
to `main` runs validation first, then publishes all three images to GHCR with
the full commit SHA and `dev` tags. The SHA
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
to the PostgreSQL Service with the platform's network policy. Staging and prod
retain the shared PostgreSQL claim template and use their cluster's default
StorageClass. Dev alone uses the `codefactory-ssd` StorageClass and a pre-bound
10Gi `ReadWriteOnce` local PV at
`/mnt/k8s-data-ssd/codefactory/postgres` on `k8s-node01`. Its class and PV both
use `Retain`; they are not the inference model-artifacts storage and must not
reuse or modify that PV, class, or data.

Before a dev deployment, verify `/mnt/k8s-data-ssd` is still the mountpoint on
`k8s-node01`. The dedicated PostgreSQL directory must be empty and owned by
UID:GID `999:999` with mode `0700`. Create only that directory if it is absent;
if it already contains anything, or the mount and permissions cannot be
verified, stop. Do not recursively change ownership or remove contents. PVCs
survive pod replacement; `Retain` also prevents automatic PV reclamation, but
is not a backup. Never delete or recreate a bound PostgreSQL PVC or PV as part
of routine deployment.

Create these namespaced secrets before deploying:

- Secret `codefactory-runtime-secrets`, with keys `POSTGRES_ADMIN_PASSWORD`,
  `GITHUB_DB_PASSWORD`, `LANGGRAPH_DB_PASSWORD`, `GITHUB_WEBHOOK_SECRET`, and
  `INTERNAL_SERVICE_TOKEN`, and `MODEL_RECONCILER_TOKEN`. The migration Job uses the admin password and both
  service passwords; each running service receives only its own DB password.
- Secret `codefactory-github-app`, with key `private-key.pem`. The GitHub
  Deployment mounts this key read-only and sets `GITHUB_PRIVATE_KEY_PATH`.

The default ConfigMap `codefactory-github-settings` supplies `GITHUB_APP_ID`.
Set the App ID before enabling GitHub delivery. Each Project item is resolved from its
webhook node ID using that event's installation; organization and user Project
owners are supported when GraphQL access is available. The linked issue
repository is checked through the same installation, and inaccessible Projects
or repositories fail closed. The default status field and option names are
`Status`, `Ready`, `In Progress`, and `Done`; optional non-secret overrides are
`GITHUB_PROJECT_STATUS_FIELD_NAME`,
`GITHUB_PROJECT_READY_OPTION_NAME`,
`GITHUB_PROJECT_IN_PROGRESS_OPTION_NAME`, and
`GITHUB_PROJECT_DONE_OPTION_NAME`.
Project owner, Project/item node IDs, field/option IDs, linked repository, and
installation context are resolved or taken from the authenticated event and
stored immutably per task. Accepted execution revalidates and updates that same
Project item to `In Progress`; completion revalidates and sets it to `Done` only
after GitHub confirms the merge and required checks. Every signed webhook must
include a positive installation ID. The GitHub
service uses that installation for later PR, check, and completion calls; there
is no static installation setting or fallback.

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
`Projects: Read` for linked Project owners and GitHub's baseline
repository metadata read. Verify these grants and selected-repository access on
the actual installation; the manifests do not create or assert them. Do not
grant a ruleset or branch-protection bypass. Proposal writes target only the
deterministic task branch and use non-force ref updates; the base branch is not
written.

The LangGraph image includes `langgraph_service.workflow.workflow_operations` as the
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
The Job runs once per created Job object. For dev, the deploy script checks
that the Job's migration copy paths match every SQL file in the checked-out
service sources. The Job pulls current service `:dev` images with
`imagePullPolicy: Always`; a missing artifact causes its init container to fail
before the migration command runs. Concurrent dev deploys are serialized by a
create-only Kubernetes Lease; an existing Lease blocks another run. A terminal
Job is deleted only with its observed UID as an API precondition, and the
script refuses to replace active or nonterminal Jobs. A stale Lease after an
interrupted runner requires an operator to verify the holder is gone before
removing that Lease.

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
an environment variable, shell command, or tracked file. The default apply
invocation validates the runtime and dashboard OAuth entries, and checks that
the private-key file exists, is non-empty, and is readable before applying all
three Secrets. For an
existing ignored `runtime-secrets.env`, do not overwrite it with the example.
It must contain only the listed service database passwords and runtime
secrets; non-secret database settings belong in the platform base or selected
environment patch. The secrets apply script requires namespace `codefactory`
to exist. By default it validates and applies all three Secrets:
`codefactory-runtime-secrets`, `codefactory-github-app`, and
`github-dashboard-oauth`.

Run the local apply mechanism from any directory:

```sh
bash infra-codefactory/kubernetes/environments/dev/secrets/apply-secrets.sh
```

To validate and apply only the dashboard OAuth Secret, run exactly:

```sh
bash infra-codefactory/kubernetes/environments/dev/secrets/apply-secrets.sh --oauth-only
```

OAuth-only mode validates and reads only local `dashboard-oauth.env`, then
applies only `github-dashboard-oauth`; it does not require or access
`runtime-secrets.env` or `github-app.pem`.

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

## Dev Deployment

Use the dev deploy script only after configuring the non-secret values in
`infra-codefactory/kubernetes/bases/platform/github-settings.yaml` and preparing
the local dev secret files above. It requires `kubectl`, `curl`, `jq`, `awk`,
`sleep`, and `mktemp` on `PATH`, plus Bash 4 or newer (`mapfile` is used).
`kubectl` must include its `kustomize` subcommand. The script uses kubectl's
client-side YAML decoder for structured preflight checks; neither `crane` nor
`yq` is required. Install the tools using the official project instructions
for your OS and architecture; this runbook does not assume a distro or package
manager:

Before deploying after any possible credential exposure, confirm the affected
provider credentials have been rotated and the local secret files contain the
replacement values. Secret metadata alone is not proof unless it establishes a
replacement after the exposure. The deploy script does not rotate provider
credentials.

- [kubectl installation](https://kubernetes.io/docs/tasks/tools/)
- [jq downloads](https://jqlang.org/download/)
- [curl downloads](https://curl.se/download.html)
- `awk`, `sleep`, and `mktemp` must be available as command-line utilities; use the
  installation source for the operating system you administer.
- [GNU Bash](https://www.gnu.org/software/bash/) documents Bash downloads and
  releases.

Check that the required commands resolve before proceeding:

```sh
for tool in bash kubectl curl jq awk sleep mktemp; do
  command -v "$tool" || exit 1
done
bash --version
kubectl version --client
jq --version
curl --version
```

The script does not build or push images, log in to a registry, call GitHub
APIs, or send webhooks/PRs. Before any apply, it renders the dev overlay and
uses kubectl's client-side decoder plus `jq` to validate the expected public
GHCR `:dev` image references, pull policies, database configuration, migration
copy paths, and retained storage contract. The migration Job copies SQL from
those service images and cannot start its migration command unless each copy
succeeds. The script recreates a terminal migration Job, then restarts the
GitHub, LangGraph, and management portal Deployments so each pulls the current
tag; it does not change replica counts. The tags are mutable: their exact image
digests can change independently of this checkout, and this dev path does not
pin digests or verify image signatures/provenance. Confirm the image publishing
workflow has completed before deploying. The API proxy is bound only to
`127.0.0.1`; it carries atomic UID-preconditioned deletes for the terminal Job
and the Lease.

Set `CODEFACTORY_DEV_CONTEXT` to the exact intended development context name.
The script prints the current context and stops unless it matches this explicit
value; it does not assume or select a context. The selected context must be
authenticated to the intended dev cluster and authorized to inspect the
namespace, StorageClasses, PersistentVolumes, migration Job, and PostgreSQL
PVC/StatefulSet; create and delete the deployment Lease; apply the dev
namespace, Secrets, and overlay; delete only a terminal migration Job; and, if
the exact legacy Pending/unbound storage state is found, delete the
UID-verified PostgreSQL StatefulSet and PVC; wait for workloads; and
port-forward the two Services. The dev overlay explicitly renders
`codefactory-ssd`, its retained static PV, and the PostgreSQL claim template.
The node-local directory must be prepared and verified before deployment.
From the repository root, run:

```sh
CODEFACTORY_DEV_CONTEXT=your-dev-context bash infra-codefactory/kubernetes/environments/dev/deploy.sh
```

Before applying anything, the script renders the dev overlay, checks the
required GitHub App and in-cluster PostgreSQL configuration without printing
secret contents, verifies readable local secret inputs, checks the mutable
`:dev` image references and migration copy paths, and inspects storage and
existing dev resources before mutation. It verifies the rendered
StorageClass/PV contract and requires any already-existing cluster objects with
those names to match it exactly; absent objects are supplied by the rendered
dev overlay. The script only removes an existing PVC/StatefulSet when the
observed PostgreSQL PVC is Pending with an empty `spec.volumeName`, both
objects have the expected claim/template identity, and their UIDs remain
unchanged. Deletions use UID preconditions and bounded waits. Any bound PVC,
non-Pending claim, unexpected class, changed UID, or mismatched identity blocks
recovery; the script never deletes a bound PVC/PV or database data. The
PostgreSQL PVC then waits to bind, followed by PostgreSQL, the migration Job,
and all three Deployments. It holds a Kubernetes Lease throughout deployment and
replaces the migration Job only when it is already Complete or Failed, using
the observed UID as a delete precondition; an active or nonterminal Job blocks
deployment. Finally, it checks
`/healthz` for both services through localhost-only port-forwards, which are
cleaned up on exit.

If the `codefactory` namespace does not exist, the script applies only the
namespace resource first, acquires the deployment Lease, invokes the existing
dev secrets apply script, and applies the rendered dev overlay.

### Non-Cluster Preflight Checks

Run the secret-free, offline dev deployment contract check from the repository
root:

```sh
bash infra-codefactory/kubernetes/environments/dev/check-deploy-contract.sh
```

The check runs `bash -n`, renders the overlay with kubectl, and confirms the
exact `:dev` image references, `Always` pull policies, and complete migration
copy lists against repository sources. It does not contact the cluster or
registry, read local secrets, or prove remote image availability or provenance.

## Ingress

The dev overlay includes an Ingress rule routing `/` and portal authentication
and API paths to Service `management-portal` on port 8000, while the exact
`/webhooks/github` path routes to Service `github-service` on port 8000. Both
services remain ClusterIP-only. Ingress class/controller, public hostname, and
HTTPS termination policy remain platform responsibilities. Configure the
matching HTTPS webhook URL in the GitHub App before enabling delivery.