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
  `INTERNAL_SERVICE_TOKEN`. The migration Job uses the admin password and both
  service passwords; each running service receives only its own DB password.
- Secret `codefactory-github-app`, with key `private-key.pem`. The GitHub
  Deployment mounts this key read-only and sets `GITHUB_PRIVATE_KEY_PATH`.

The default ConfigMap `codefactory-github-settings` supplies `GITHUB_APP_ID`.
Set the App ID before enabling GitHub delivery. Each Project item is resolved from its
webhook node ID using that event's installation; organization and user Project
owners are supported when GraphQL access is available. The linked issue
repository is checked through the same installation, and inaccessible Projects
or repositories fail closed. The default status field and option names are
`Status`, `Ready`, and `Done`; optional non-secret overrides are
`GITHUB_PROJECT_STATUS_FIELD_NAME`,
`GITHUB_PROJECT_READY_OPTION_NAME`, and `GITHUB_PROJECT_DONE_OPTION_NAME`.
Project owner, Project/item node IDs, field/option IDs, linked repository, and
installation context are resolved or taken from the authenticated event and
stored immutably per task. Completion revalidates and updates that same Project
item. Every signed webhook must include a positive installation ID. The GitHub
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
The Job runs once per created Job object. The dev deploy script derives its SQL
artifact checks from the Job's init-container copy commands. It resolves both
public service tags to digests, inspects those digest references, and applies
the same digest-pinned rendered manifest. Concurrent dev deploys are serialized
by a create-only Kubernetes Lease; an existing Lease blocks another run. A
terminal Job is deleted only with its observed UID as an API precondition, and
the script refuses to replace active or nonterminal Jobs. A stale Lease after
an interrupted runner requires an operator to verify the holder is gone before
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
the local dev secret files above. It requires `kubectl`, `crane`, `curl`, `jq`,
`tar`, and `awk` on `PATH`, plus Bash 4 or newer (`mapfile` is used). `kubectl`
must include its `kustomize` subcommand. Mike Farah `yq` v4, `sleep`, and
`mktemp` are also required for structured digest pinning, bounded waits, and
temporary files. Install the tools using the official
project instructions for your OS and architecture; this runbook does not assume
a distro or package manager:

- [kubectl installation](https://kubernetes.io/docs/tasks/tools/)
- [crane installation](https://github.com/google/go-containerregistry/tree/main/cmd/crane)
- [jq downloads](https://jqlang.org/download/)
- [Mike Farah yq](https://github.com/mikefarah/yq)
- [curl downloads](https://curl.se/download.html)
- `tar`, `awk`, `sleep`, and `mktemp` must be available as command-line utilities; use the
  installation source for the operating system you administer.
- [GNU Bash](https://www.gnu.org/software/bash/) documents Bash downloads and
  releases.

Check that the required commands resolve before proceeding:

```sh
for tool in bash kubectl crane curl jq yq tar awk sleep mktemp; do
  command -v "$tool" || exit 1
done
bash --version
kubectl version --client
crane version
jq --version
yq --version
curl --version
```

The script does not build or push images, log in to a registry, call GitHub
APIs, or send webhooks/PRs. Before any apply, it resolves the public GHCR tags
with `crane digest`, pins every service and migration Job image field in the
rendered YAML with `yq`, exports each image by digest, and verifies all SQL
paths copied by the migration Job. It then applies that exact pinned render,
not the mutable-tag Kustomize output. If a file is missing, publish updated dev
images with the repository's **Build and publish service images** workflow
before retrying. The API proxy is bound only to `127.0.0.1`; it carries atomic
UID-preconditioned deletes for the terminal Job and the Lease.

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
secret contents, verifies readable local secret inputs, resolves and pins both
service image digests, checks every migration SQL artifact, and inspects
storage and existing dev resources before mutation. It verifies the rendered
StorageClass/PV contract and requires any already-existing cluster objects with
those names to match it exactly; absent objects are supplied by the rendered
dev overlay. The script only removes an existing PVC/StatefulSet when the
observed PostgreSQL PVC is Pending with an empty `spec.volumeName`, both
objects have the expected claim/template identity, and their UIDs remain
unchanged. Deletions use UID preconditions and bounded waits. Any bound PVC,
non-Pending claim, unexpected class, changed UID, or mismatched identity blocks
recovery; the script never deletes a bound PVC/PV or database data. The
PostgreSQL PVC then waits to bind, followed by PostgreSQL, the migration Job,
and both Deployments. It holds a Kubernetes Lease throughout deployment and
replaces the migration Job only when it is already Complete or Failed, using
the observed UID as a delete precondition; an active or nonterminal Job blocks
deployment. Finally, it checks
`/healthz` for both services through localhost-only port-forwards, which are
cleaned up on exit.

If the `codefactory` namespace does not exist, the script applies only the
namespace resource first, acquires the deployment Lease, invokes the existing
dev secrets apply script, and applies the digest-pinned render.

### Non-Cluster Preflight Checks

The following mock check runs from the repository root and requires the
prepared dev secret files because the deploy script validates their presence
and shape. It does not read or print their values. The real `kubectl` binary is
used once to render the overlay locally before the mock is installed; all
`kubectl` calls made by the deploy script are mocked. Mike Farah `yq` v4 is
used for the structured reads/pinning, while `crane` is mocked, so neither the
cluster nor GHCR is contacted. The mock rejects every unexpected `kubectl`
command and checks that resource mutation, proxy, and port-forward commands
were not invoked. In addition to Bash, the snippet uses `cat`, `chmod`, `grep`,
`mkdir`, `mktemp`, and `touch`.

```sh
real_kubectl="$(command -v kubectl)"
ulimit -c 0
mock_root="$(mktemp -d)"
trap 'rm -rf "$mock_root"' EXIT
"$real_kubectl" kustomize infra-codefactory/kubernetes/environments/dev \
  > "$mock_root/dev.yaml"
mkdir -p "$mock_root/bin" "$mock_root/artifacts/app/migrations"
touch "$mock_root/artifacts/app/migrations/001_github_event_queue.sql"
touch "$mock_root/artifacts/app/migrations/002_service_roles.sql"
touch "$mock_root/artifacts/app/migrations/003_github_task_installation.sql"
touch "$mock_root/artifacts/app/migrations/001_checkpoint_schema.sql"
cat > "$mock_root/bin/kubectl" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$MOCK_CALL_LOG"
case "$*" in
  'config current-context') printf '%s\n' "$CODEFACTORY_DEV_CONTEXT" ;;
  kustomize\ *) cat "$MOCK_RENDERED_MANIFEST" ;;
  'get storageclass codefactory-ssd -o json --ignore-not-found') printf '%s' "$MOCK_STORAGE_CLASS_JSON" ;;
  'get pv codefactory-postgres-data -o json --ignore-not-found') printf '%s' "$MOCK_PV_JSON" ;;
  *) printf 'Unexpected kubectl call: %s\n' "$*" >&2; exit 97 ;;
esac
MOCK
cat > "$mock_root/bin/crane" <<'MOCK'
#!/usr/bin/env bash
case "$1" in
  digest) printf 'sha256:%064d\n' 1 ;;
  export)
    artifacts=()
    for path in \
      app/migrations/001_github_event_queue.sql \
      app/migrations/002_service_roles.sql \
      app/migrations/003_github_task_installation.sql \
      app/migrations/001_checkpoint_schema.sql; do
      [[ "$path" == "$MOCK_MISSING_ARTIFACT" ]] || artifacts+=("$path")
    done
    tar -cf - -C "$MOCK_ROOT/artifacts" "${artifacts[@]}"
    ;;
  *) exit 98 ;;
esac
MOCK
chmod +x "$mock_root/bin/kubectl" "$mock_root/bin/crane"
export MOCK_ROOT="$mock_root"
export MOCK_CALL_LOG="$mock_root/kubectl.log"
export MOCK_RENDERED_MANIFEST="$mock_root/dev.yaml"
export CODEFACTORY_DEV_CONTEXT=mock-dev-context

run_preflight_case() {
  local case_name="$1"
  local missing_artifact="$2"
  local expected="$3"
  local storage_class_json="${4:-}"
  local pv_json="${5:-}"
  : > "$MOCK_CALL_LOG"
  if PATH="$mock_root/bin:$PATH" MOCK_MISSING_ARTIFACT="$missing_artifact" \
    MOCK_STORAGE_CLASS_JSON="$storage_class_json" MOCK_PV_JSON="$pv_json" bash \
    infra-codefactory/kubernetes/environments/dev/deploy.sh \
    > "$mock_root/$case_name.log" 2>&1; then
    printf 'FAIL: %s unexpectedly passed\n' "$case_name" >&2
    return 1
  fi
  if ! grep -F "$expected" "$mock_root/$case_name.log"; then
    printf 'FAIL: %s did not report the expected preflight error\n' "$case_name" >&2
    return 1
  fi
  if grep -Eq '(^|[[:space:]])(apply|create|delete|proxy|port-forward)([[:space:]]|$)' "$MOCK_CALL_LOG"; then
    printf 'FAIL: %s invoked a forbidden kubectl operation\n' "$case_name" >&2
    return 1
  fi
  printf 'PASS: %s stopped before apply/delete/port-forward\n' "$case_name"
}

run_preflight_case missing-github-event-queue \
  app/migrations/001_github_event_queue.sql 'lacks app/migrations/001_github_event_queue.sql'
run_preflight_case missing-service-roles \
  app/migrations/002_service_roles.sql 'lacks app/migrations/002_service_roles.sql'
run_preflight_case missing-task-installation \
  app/migrations/003_github_task_installation.sql 'lacks app/migrations/003_github_task_installation.sql'
run_preflight_case missing-checkpoint-schema \
  app/migrations/001_checkpoint_schema.sql 'lacks app/migrations/001_checkpoint_schema.sql'
run_preflight_case mismatched-storage-class '' \
  'Live StorageClass codefactory-ssd does not exactly match the dev overlay contract.' \
  '{"metadata":{"name":"codefactory-ssd"},"provisioner":"unexpected"}' ''
valid_storage_class='{"apiVersion":"storage.k8s.io/v1","kind":"StorageClass","metadata":{"name":"codefactory-ssd"},"provisioner":"kubernetes.io/no-provisioner","reclaimPolicy":"Retain","volumeBindingMode":"WaitForFirstConsumer"}'
run_preflight_case mismatched-postgres-volume '' \
  'Live PersistentVolume codefactory-postgres-data does not exactly match the dev overlay contract.' \
  "$valid_storage_class" '{"metadata":{"name":"codefactory-postgres-data"},"spec":{}}'
```

Expected results are `PASS` for all six cases: each of the four SQL artifacts
is independently required, and mismatched live StorageClass and PV definitions
are rejected. Missing-artifact cases stop before any StorageClass query. Any
unexpected Kubernetes command, or any attempt to mutate resources, start a
proxy, or port-forward, fails the check. These mock results validate preflight
ordering only; they do not prove cluster access, storage provisioning, image
availability, or deployment readiness.

## Webhook Ingress Prerequisite

Ingress and HTTPS termination are platform/environment responsibilities, not
external application services. No Ingress is included because a public
hostname, TLS secret/certificate policy, and Ingress class/controller were not
provided. Before enabling GitHub delivery, configure the platform-owned Ingress
to route `POST /webhooks/github` to Service `github-service` on port 8000, then
set the matching HTTPS webhook URL in the GitHub App. The service itself stays
ClusterIP-only.