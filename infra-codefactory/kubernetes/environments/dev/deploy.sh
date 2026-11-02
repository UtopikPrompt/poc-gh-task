#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/../../../.." && pwd)"
cd "$repo_root"
overlay_dir="$script_dir"
namespace_file="$repo_root/infra-codefactory/kubernetes/bases/platform/namespace.yaml"
secrets_script="$script_dir/secrets/apply-secrets.sh"
runtime_env="$script_dir/secrets/runtime-secrets.env"
private_key_file="$script_dir/secrets/github-app.pem"
namespace=codefactory
migration_job=codefactory-database-migrations
postgres_statefulset=codefactory-postgres
postgres_pvc=postgres-data-codefactory-postgres-0
work_dir=""
port_forward_pid=""
api_proxy_pid=""
api_port=""
lock_uid=""
lock_name=codefactory-dev-deploy-lock

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM
  if [[ -n "$lock_uid" && -n "$api_port" ]]; then
    curl -fsS --max-time 10 -X DELETE \
      -H 'Content-Type: application/json' \
      --data-binary "{\"apiVersion\":\"v1\",\"kind\":\"DeleteOptions\",\"preconditions\":{\"uid\":\"$lock_uid\"}}" \
      "http://127.0.0.1:$api_port/apis/coordination.k8s.io/v1/namespaces/$namespace/leases/$lock_name" \
      >/dev/null 2>&1 || printf 'Warning: deployment Lease was not released; inspect it before removing it.\n' >&2
  fi
  if [[ -n "$port_forward_pid" ]]; then
    kill "$port_forward_pid" >/dev/null 2>&1 || true
    wait "$port_forward_pid" >/dev/null 2>&1 || true
  fi
  if [[ -n "$api_proxy_pid" ]]; then
    kill "$api_proxy_pid" >/dev/null 2>&1 || true
    wait "$api_proxy_pid" >/dev/null 2>&1 || true
  fi
  if [[ -n "$work_dir" ]]; then
    rm -rf "$work_dir"
  fi
  exit "$exit_code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

fail() {
  printf 'Preflight/deploy error: %s\n' "$1" >&2
  exit 1
}

for tool in bash kubectl curl jq awk sleep mktemp; do
  command -v "$tool" >/dev/null 2>&1 || fail "Required tool '$tool' is not installed or not on PATH."
done
(( BASH_VERSINFO[0] >= 4 )) || fail "Bash 4 or newer is required."
work_dir="$(mktemp -d)" || fail "Unable to create a temporary work directory."

current_context="$(kubectl config current-context 2>/dev/null)" || fail "kubectl has no usable current context."
[[ -n "$current_context" ]] || fail "kubectl returned an empty current context."
printf 'Current kubectl context: %s\n' "$current_context"
[[ -n "${CODEFACTORY_DEV_CONTEXT:-}" ]] || fail "Set CODEFACTORY_DEV_CONTEXT to the exact dev context name before running this noninteractive deploy."
[[ "$current_context" == "$CODEFACTORY_DEV_CONTEXT" ]] || fail "Current context does not match CODEFACTORY_DEV_CONTEXT. No resources were changed."

rendered_manifest="$work_dir/dev.yaml"
kubectl kustomize "$overlay_dir" > "$rendered_manifest" || fail "Unable to render the dev overlay."
rendered_json="$work_dir/dev.json"
kubectl create --dry-run=client -f "$rendered_manifest" -o json | jq -s '.' > "$rendered_json" \
  || fail "Unable to convert the rendered dev overlay to structured JSON with kubectl."

config_value() {
  local resource_name="$1"
  local key="$2"
  jq -r --arg resource "$resource_name" --arg key "$key" \
    '[.[] | select(.kind == "ConfigMap" and .metadata.name == $resource) | .data[$key] // empty] | .[0] // empty' \
    "$rendered_json"
}

github_app_id="$(config_value codefactory-github-settings GITHUB_APP_ID)"
[[ "$github_app_id" =~ ^[1-9][0-9]*$ ]] || fail "Dev GitHub App ID must be configured as a positive integer."

for key in POSTGRES_ADMIN_USERNAME GITHUB_DB_HOST GITHUB_DB_SSLMODE GITHUB_DB_USERNAME GITHUB_DB_PORT GITHUB_DB_NAME LANGGRAPH_DB_HOST LANGGRAPH_DB_SSLMODE LANGGRAPH_DB_USERNAME LANGGRAPH_DB_PORT LANGGRAPH_DB_NAME; do
  [[ -n "$(config_value codefactory-database-config "$key")" ]] || fail "Rendered dev database config is missing $key."
done
[[ "$(config_value codefactory-database-config GITHUB_DB_HOST)" == codefactory-postgres && "$(config_value codefactory-database-config LANGGRAPH_DB_HOST)" == codefactory-postgres ]] || fail "Both dev database hosts must use the in-cluster codefactory-postgres Service."
[[ "$(config_value codefactory-database-config GITHUB_DB_PORT)" == 5432 && "$(config_value codefactory-database-config LANGGRAPH_DB_PORT)" == 5432 ]] || fail "Both dev database ports must match PostgreSQL Service port 5432."
[[ "$(config_value codefactory-database-config GITHUB_DB_SSLMODE)" == disable && "$(config_value codefactory-database-config LANGGRAPH_DB_SSLMODE)" == disable ]] || fail "Dev database SSL mode must match the in-cluster PostgreSQL configuration."

if [[ ! -f "$runtime_env" || ! -r "$runtime_env" ]]; then
  fail "Dev runtime-secrets.env is missing or unreadable. Prepare the local secrets file before deployment."
fi
if [[ ! -f "$private_key_file" || ! -s "$private_key_file" || ! -r "$private_key_file" ]]; then
  fail "Dev GitHub App private-key file is missing, empty, or unreadable."
fi
if ! awk -F= '
  BEGIN {
    allowed["POSTGRES_ADMIN_PASSWORD"] = 1
    allowed["GITHUB_DB_PASSWORD"] = 1
    allowed["LANGGRAPH_DB_PASSWORD"] = 1
    allowed["GITHUB_WEBHOOK_SECRET"] = 1
    allowed["INTERNAL_SERVICE_TOKEN"] = 1
  }
  /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
  !($1 in allowed) { failed = 1 }
  END { exit failed }
' "$runtime_env"; then
  fail "Dev runtime-secrets.env contains an unsupported key. No secret values were displayed."
fi
for key in POSTGRES_ADMIN_PASSWORD GITHUB_DB_PASSWORD LANGGRAPH_DB_PASSWORD GITHUB_WEBHOOK_SECRET INTERNAL_SERVICE_TOKEN; do
  if ! awk -F= -v key="$key" '$1 == key && length(substr($0, index($0, "=") + 1)) > 0 { found = 1 } END { exit !found }' "$runtime_env"; then
    fail "Dev runtime-secrets.env must define a non-empty $key entry. No secret values were displayed."
  fi
done
[[ -r "$secrets_script" ]] || fail "The existing dev secrets apply script is missing or unreadable."

github_tag_ref=ghcr.io/utopikprompt/poc-gh-task-github-service:dev
langgraph_tag_ref=ghcr.io/utopikprompt/poc-gh-task-langgraph-service:dev
portal_tag_ref=ghcr.io/utopikprompt/poc-gh-task-management-portal:dev
jq -e --arg github "$github_tag_ref" --arg langgraph "$langgraph_tag_ref" '
  def containers: (.spec.template.spec // {}) | ((.initContainers // []) + (.containers // []));
  [ .[] | select(.kind == "Deployment" and .metadata.name == "github-service") | containers[] | select(.name == "github-service" and .image == $github and .imagePullPolicy == "Always") ] | length == 1
' "$rendered_json" >/dev/null || fail "The rendered GitHub Deployment must use its :dev image with imagePullPolicy Always."
jq -e --arg image "$langgraph_tag_ref" '
  def containers: (.spec.template.spec // {}) | ((.initContainers // []) + (.containers // []));
  [ .[] | select((.kind == "Deployment" and .metadata.name == "langgraph-service") or (.kind == "Job" and .metadata.name == "codefactory-database-migrations")) | containers[] | select(.image == $image and .imagePullPolicy == "Always") ] | length == 3
' "$rendered_json" >/dev/null || fail "The rendered LangGraph Deployment and migration Job must use their :dev image with imagePullPolicy Always."
jq -e --arg image "$github_tag_ref" '
  [ .[] | select(.kind == "Job" and .metadata.name == "codefactory-database-migrations") | .spec.template.spec.initContainers[] | select(.name == "copy-github-migrations" and .image == $image and .imagePullPolicy == "Always") ] | length == 1
' "$rendered_json" >/dev/null || fail "The migration Job must pull the GitHub :dev image with imagePullPolicy Always."
jq -e --arg image "$portal_tag_ref" '
  [ .[] | select(.kind == "Deployment" and .metadata.name == "management-portal") | .spec.template.spec.containers[] | select(.image == $image and .imagePullPolicy == "Always") ] | length == 1
' "$rendered_json" >/dev/null || fail "The rendered management portal must use its :dev image with imagePullPolicy Always."

declare -a github_migrations=()
declare -a langgraph_migrations=()
github_init_seen=false
langgraph_init_seen=false
while IFS=$'\t' read -r container_name args; do
  case "$container_name" in
    copy-github-migrations) target_migrations=github ; github_init_seen=true ;;
    copy-langgraph-migration) target_migrations=langgraph ; langgraph_init_seen=true ;;
    *) fail "Unrecognized migration Job init container '$container_name'; review migration artifact preflight." ;;
  esac
  while [[ "$args" =~ /app/migrations/([A-Za-z0-9_.-]+\.sql) ]]; do
    migration_file="${BASH_REMATCH[1]}"
    migration_path="app/migrations/$migration_file"
    if [[ "$target_migrations" == github ]]; then
      [[ " ${github_migrations[*]} " != *" $migration_path "* ]] || fail "GitHub migration Job repeats $migration_path."
      [[ -f "$repo_root/codefactory/github-service/migrations/$migration_file" ]] || fail "The GitHub migration Job references missing repository source $migration_path."
      github_migrations+=("$migration_path")
    else
      [[ " ${langgraph_migrations[*]} " != *" $migration_path "* ]] || fail "LangGraph migration Job repeats $migration_path."
      [[ -f "$repo_root/codefactory/langgraph-service/migrations/$migration_file" ]] || fail "The LangGraph migration Job references missing repository source $migration_path."
      langgraph_migrations+=("$migration_path")
    fi
    args="${args#*"/app/migrations/$migration_file"}"
  done
done < <(jq -r '.[] | select(.kind == "Job" and .metadata.name == "codefactory-database-migrations") | .spec.template.spec.initContainers[] | [.name, (.args // [] | join(" "))] | @tsv' "$rendered_json")
[[ "$github_init_seen" == true && "${#github_migrations[@]}" -gt 0 ]] || fail "Unable to derive GitHub migration SQL paths from the rendered migration Job."
[[ "$langgraph_init_seen" == true && "${#langgraph_migrations[@]}" -gt 0 ]] || fail "Unable to derive LangGraph migration SQL paths from the rendered migration Job."

verify_migration_set() {
  local source_dir="$1"
  local declared_name="$2"
  shift 2
  local source_file
  local migration_path
  local -a declared=("$@")
  local source_count=0
  for source_file in "$source_dir"/*.sql; do
    [[ -f "$source_file" ]] || continue
    ((source_count += 1))
    migration_path="app/migrations/$(basename "$source_file")"
    [[ " ${declared[*]} " == *" $migration_path "* ]] || fail "$declared_name migration Job omits repository migration $migration_path."
  done
  [[ "$source_count" == "${#declared[@]}" ]] || fail "$declared_name migration Job and repository migration file counts do not match."
}
verify_migration_set "$repo_root/codefactory/github-service/migrations" GitHub "${github_migrations[@]}"
verify_migration_set "$repo_root/codefactory/langgraph-service/migrations" LangGraph "${langgraph_migrations[@]}"

storage_class=codefactory-ssd
rendered_storage_class_json="$(jq -ce '[.[] | select(.kind == "StorageClass" and .metadata.name == "codefactory-ssd")] | if length == 1 then .[0] else error("expected one StorageClass") end' "$rendered_json")" || fail "The dev overlay must render exactly one StorageClass named $storage_class."
rendered_postgres_volume_json="$(jq -ce '[.[] | select(.kind == "PersistentVolume" and .metadata.name == "codefactory-postgres-data")] | if length == 1 then .[0] else error("expected one PersistentVolume") end' "$rendered_json")" || fail "The dev overlay must render exactly one PersistentVolume named codefactory-postgres-data."
rendered_claim_class="$(jq -r '[.[] | select(.kind == "StatefulSet" and .metadata.name == "codefactory-postgres") | .spec.volumeClaimTemplates[] | select(.metadata.name == "postgres-data") | .spec.storageClassName // ""] | .[0] // empty' "$rendered_json")" || fail "Unable to read the rendered PostgreSQL claim template."
[[ "$rendered_claim_class" == "$storage_class" ]] || fail "The dev PostgreSQL claim template must use StorageClass $storage_class."
jq -e '
  .apiVersion == "storage.k8s.io/v1" and .kind == "StorageClass" and
  (.metadata | keys == ["name"] and .name == "codefactory-ssd") and
  .provisioner == "kubernetes.io/no-provisioner" and
  .reclaimPolicy == "Retain" and
  .volumeBindingMode == "WaitForFirstConsumer" and
  (keys == ["apiVersion", "kind", "metadata", "provisioner", "reclaimPolicy", "volumeBindingMode"])
' <<< "$rendered_storage_class_json" >/dev/null || fail "The rendered PostgreSQL StorageClass does not match the required retained, non-default local-storage contract."
jq -e '
  .apiVersion == "v1" and .kind == "PersistentVolume" and
  .metadata.name == "codefactory-postgres-data" and
  (.spec | keys == ["accessModes", "capacity", "claimRef", "local", "nodeAffinity", "persistentVolumeReclaimPolicy", "storageClassName", "volumeMode"]) and
  .spec.capacity == {"storage":"10Gi"} and
  .spec.volumeMode == "Filesystem" and
  .spec.accessModes == ["ReadWriteOnce"] and
  .spec.persistentVolumeReclaimPolicy == "Retain" and
  .spec.storageClassName == "codefactory-ssd" and
  .spec.local.path == "/mnt/k8s-data-ssd/codefactory/postgres" and
  .spec.nodeAffinity.required.nodeSelectorTerms == [{"matchExpressions":[{"key":"kubernetes.io/hostname","operator":"In","values":["k8s-node01"]}]}] and
  .spec.claimRef == {"namespace":"codefactory","name":"postgres-data-codefactory-postgres-0"}
' <<< "$rendered_postgres_volume_json" >/dev/null || fail "The rendered PostgreSQL PersistentVolume does not match the required retained local SSD contract."

live_storage_class_json="$(kubectl get storageclass "$storage_class" -o json --ignore-not-found)" || fail "Unable to inspect StorageClass $storage_class; no resources were changed."
if [[ -n "$live_storage_class_json" ]]; then
  jq -e --argjson expected "$rendered_storage_class_json" '
    def contract: {
      name: .metadata.name,
      provisioner: .provisioner,
      reclaimPolicy: .reclaimPolicy,
      volumeBindingMode: .volumeBindingMode,
      parameters: (.parameters // {}),
      mountOptions: (.mountOptions // []),
      allowVolumeExpansion: (.allowVolumeExpansion // false),
      allowedTopologies: (.allowedTopologies // []),
      isDefault: (((.metadata.annotations // {})["storageclass.kubernetes.io/is-default-class"] // (.metadata.annotations // {})["storageclass.beta.kubernetes.io/is-default-class"] // "false") == "true")
    };
    contract == ($expected | contract)
  ' <<< "$live_storage_class_json" >/dev/null || fail "Live StorageClass $storage_class does not exactly match the dev overlay contract."
fi
live_postgres_volume_json="$(kubectl get pv codefactory-postgres-data -o json --ignore-not-found)" || fail "Unable to inspect PersistentVolume codefactory-postgres-data; no resources were changed."
if [[ -n "$live_postgres_volume_json" ]]; then
  jq -e --argjson expected "$rendered_postgres_volume_json" '
    def contract: {
      name: .metadata.name,
      specKeys: (.spec | keys),
      capacity: .spec.capacity,
      volumeMode: .spec.volumeMode,
      accessModes: .spec.accessModes,
      reclaimPolicy: .spec.persistentVolumeReclaimPolicy,
      storageClassName: .spec.storageClassName,
      localPath: .spec.local.path,
      nodeAffinity: .spec.nodeAffinity,
      claimNamespace: .spec.claimRef.namespace,
      claimName: .spec.claimRef.name,
      claimRefExtraKeys: ((.spec.claimRef | keys) - ["apiVersion", "fieldPath", "kind", "name", "namespace", "resourceVersion", "uid"])
    };
    contract == ($expected | contract)
  ' <<< "$live_postgres_volume_json" >/dev/null || fail "Live PersistentVolume codefactory-postgres-data does not exactly match the dev overlay contract."
fi
printf 'PostgreSQL StorageClass and PersistentVolume preflight passed: %s\n' "$storage_class"

bound_postgres_claim_is_reusable() {
  local pvc_json="$1"
  local pv_json="$2"
  local storage_class_json="$3"
  local pvc_uid
  pvc_uid="$(jq -r '.metadata.uid // empty' <<< "$pvc_json")"
  [[ -n "$pvc_uid" ]] || return 1
  jq -e --arg namespace "$namespace" --arg pvc "$postgres_pvc" --arg pv "codefactory-postgres-data" --arg uid "$pvc_uid" '
    .metadata.name == $pvc and .metadata.namespace == $namespace and .metadata.uid == $uid and
    .status.phase == "Bound" and .spec.volumeName == $pv and
    ((.spec.storageClassName // "") == "") and
    .spec.accessModes == ["ReadWriteOnce"] and (.spec.volumeMode // "Filesystem") == "Filesystem" and
    .spec.resources.requests.storage == "10Gi" and .status.capacity.storage == "10Gi"
  ' <<< "$pvc_json" >/dev/null && jq -e --arg namespace "$namespace" --arg pvc "$postgres_pvc" --arg uid "$pvc_uid" --arg storage_class "$storage_class" '
    .metadata.name == "codefactory-postgres-data" and .status.phase == "Bound" and
    .spec.claimRef.namespace == $namespace and .spec.claimRef.name == $pvc and .spec.claimRef.uid == $uid and
    .spec.capacity.storage == "10Gi" and .spec.volumeMode == "Filesystem" and
    .spec.accessModes == ["ReadWriteOnce"] and .spec.persistentVolumeReclaimPolicy == "Retain" and
    .spec.storageClassName == $storage_class and
    .spec.local.path == "/mnt/k8s-data-ssd/codefactory/postgres" and
    .spec.nodeAffinity.required.nodeSelectorTerms == [{"matchExpressions":[{"key":"kubernetes.io/hostname","operator":"In","values":["k8s-node01"]}]}]
  ' <<< "$pv_json" >/dev/null && jq -e --arg storage_class "$storage_class" '
    .metadata.name == $storage_class and .provisioner == "kubernetes.io/no-provisioner" and
    .reclaimPolicy == "Retain" and .volumeBindingMode == "WaitForFirstConsumer"
  ' <<< "$storage_class_json" >/dev/null
}

postgres_storage_identity_is_recoverable() {
  local pvc_json="$1"
  local statefulset_json="$2"
  jq -e --arg namespace "$namespace" --arg pvc "$postgres_pvc" --arg storage_class "$storage_class" '
    .metadata.name == $pvc and .metadata.namespace == $namespace and
    (.metadata.uid | type == "string" and length > 0) and
    .metadata.labels["app.kubernetes.io/name"] == "codefactory-postgres" and
    ((.metadata.ownerReferences // []) | length == 0) and
    .status.phase == "Pending" and (.spec.volumeName // "") == "" and
    .spec.accessModes == ["ReadWriteOnce"] and
    .spec.resources.requests.storage == "10Gi" and
    ((.spec.storageClassName // "") == "" or .spec.storageClassName == $storage_class)
  ' <<< "$pvc_json" >/dev/null && jq -e --arg namespace "$namespace" --arg storage_class "$storage_class" '
    .metadata.name == "codefactory-postgres" and .metadata.namespace == $namespace and
    (.metadata.uid | type == "string" and length > 0) and
    .spec.replicas == 1 and
    .spec.selector.matchLabels == {"app.kubernetes.io/name":"codefactory-postgres"} and
    .spec.template.metadata.labels["app.kubernetes.io/name"] == "codefactory-postgres" and
    (.spec.volumeClaimTemplates | length == 1) and
    .spec.volumeClaimTemplates[0].metadata.name == "postgres-data" and
    .spec.volumeClaimTemplates[0].spec.accessModes == ["ReadWriteOnce"] and
    .spec.volumeClaimTemplates[0].spec.resources.requests.storage == "10Gi" and
    ((.spec.volumeClaimTemplates[0].spec.storageClassName // "") == "" or .spec.volumeClaimTemplates[0].spec.storageClassName == $storage_class)
  ' <<< "$statefulset_json" >/dev/null
}

namespace_json="$(kubectl get namespace "$namespace" -o json --ignore-not-found)" || fail "Unable to inspect namespace $namespace; no resources were changed."
namespace_exists=true
if [[ -z "$namespace_json" ]]; then
  namespace_exists=false
fi
live_volume_phase=Available
if [[ -n "$live_postgres_volume_json" ]]; then
  live_volume_phase="$(jq -r '.status.phase // "Available"' <<< "$live_postgres_volume_json")"
  [[ "$live_volume_phase" == Available || "$live_volume_phase" == Bound ]] || fail "PersistentVolume codefactory-postgres-data is $live_volume_phase; retained data will not be adopted or changed."
  if [[ "$live_volume_phase" == Bound && "$namespace_exists" == false ]]; then
    fail "PersistentVolume codefactory-postgres-data is Bound but namespace $namespace is absent; retained data will not be adopted."
  fi
fi

pvc_json=""
statefulset_json=""
existing_pvc_uid=""
existing_statefulset_uid=""
needs_storage_recovery=false
recreate_pvc=false
if [[ "$namespace_exists" == true ]]; then
  pvc_json="$(kubectl -n "$namespace" get pvc "$postgres_pvc" -o json --ignore-not-found)" || fail "Unable to inspect the existing PostgreSQL PVC; no resources were changed."
  statefulset_json="$(kubectl -n "$namespace" get statefulset "$postgres_statefulset" -o json --ignore-not-found)" || fail "Unable to inspect the existing PostgreSQL StatefulSet; no resources were changed."
  if [[ -n "$live_postgres_volume_json" && "$live_volume_phase" == Bound ]]; then
      [[ -n "$pvc_json" ]] || fail "PersistentVolume codefactory-postgres-data is Bound but the expected PostgreSQL PVC is absent; retained data will not be adopted."
      [[ "$(jq -r '.metadata.uid // empty' <<< "$pvc_json")" == "$(jq -r '.spec.claimRef.uid // empty' <<< "$live_postgres_volume_json")" ]] || fail "PersistentVolume codefactory-postgres-data is Bound to a different PostgreSQL PVC identity; retained data will not be adopted."
      [[ "$(jq -r '.spec.volumeName // ""' <<< "$pvc_json")" == "codefactory-postgres-data" && "$(jq -r '.status.phase // ""' <<< "$pvc_json")" == Bound ]] || fail "PersistentVolume codefactory-postgres-data is Bound but the expected PostgreSQL PVC does not confirm that binding."
  fi
  if [[ -n "$pvc_json" ]]; then
    existing_storage_class="$(jq -r '.spec.storageClassName // ""' <<< "$pvc_json")"
    existing_pvc_uid="$(jq -r '.metadata.uid // empty' <<< "$pvc_json")"
    if [[ "$existing_storage_class" != "$storage_class" ]]; then
      [[ -z "$existing_storage_class" ]] || fail "Existing PostgreSQL PVC uses a different StorageClass than the rendered dev configuration. No PVC will be replaced or adopted."
      if [[ "$(jq -r '.status.phase // ""' <<< "$pvc_json")" == Bound ]]; then
        bound_postgres_claim_is_reusable "$pvc_json" "$live_postgres_volume_json" "$live_storage_class_json" \
          || fail "Bound classless PostgreSQL PVC does not match the retained PV identity, capacity, mode, local path, and driver contract. No storage resources will be changed."
        printf 'Existing bound PostgreSQL PVC identity and retained local storage verified; the PVC, PV, and StatefulSet will be preserved.\n'
      else
        needs_storage_recovery=true
        recreate_pvc=true
      fi
    fi
  fi
  if [[ -n "$statefulset_json" ]]; then
    existing_statefulset_uid="$(jq -r '.metadata.uid // empty' <<< "$statefulset_json")"
    existing_template_class="$(jq -r '.spec.volumeClaimTemplates[] | select(.metadata.name == "postgres-data") | .spec.storageClassName // ""' <<< "$statefulset_json")"
    if [[ "$existing_template_class" != "$storage_class" ]]; then
      [[ -z "$existing_template_class" ]] || fail "Existing PostgreSQL StatefulSet claim template uses an unexpected StorageClass. No StatefulSet or PVC will be replaced."
      needs_storage_recovery=true
    fi
  fi
  if [[ "$needs_storage_recovery" == true ]]; then
    [[ -n "$pvc_json" && -n "$statefulset_json" ]] || fail "PostgreSQL storage differs from the dev render, but the expected StatefulSet and PVC identity pair is incomplete. No StatefulSet or PVC will be replaced."
    postgres_storage_identity_is_recoverable "$pvc_json" "$statefulset_json" || fail "PostgreSQL storage differs from the dev render, but the PVC is not the expected Pending, unbound 10Gi claim or the StatefulSet/PVC identity changed. No StatefulSet or PVC will be replaced."
    [[ -n "$existing_pvc_uid" && -n "$existing_statefulset_uid" ]] || fail "The PostgreSQL StatefulSet or PVC has no UID. No StatefulSet or PVC will be replaced."
    printf 'PostgreSQL storage recovery is eligible for the observed Pending, unbound claim (PVC recreation: %s).\n' "$recreate_pvc"
  fi
  initial_job_json="$(kubectl -n "$namespace" get job "$migration_job" -o json --ignore-not-found)" || fail "Unable to inspect the existing migration Job; no resources were changed."
  if [[ -n "$initial_job_json" ]]; then
    if jq -e '(.status.active // 0) > 0' >/dev/null <<< "$initial_job_json"; then
      fail "Migration Job $migration_job is active. It will not be deleted or replaced."
    elif ! jq -e '[.status.conditions[]? | select((.type == "Complete" or .type == "Failed") and .status == "True")] | length > 0' >/dev/null <<< "$initial_job_json"; then
      fail "Migration Job $migration_job is not terminal. It will not be deleted or replaced."
    fi
    [[ -n "$(jq -r '.metadata.uid // empty' <<< "$initial_job_json")" ]] || fail "Existing migration Job $migration_job has no UID; it will not be replaced."
  fi
fi

[[ "$(kubectl auth can-i create leases.coordination.k8s.io --namespace="$namespace")" == yes ]] || fail "The selected context cannot create the deployment Lease in namespace $namespace."
[[ "$(kubectl auth can-i delete leases.coordination.k8s.io --namespace="$namespace")" == yes ]] || fail "The selected context cannot release the deployment Lease in namespace $namespace."
[[ "$(kubectl auth can-i delete jobs.batch --namespace="$namespace")" == yes ]] || fail "The selected context cannot replace the terminal migration Job in namespace $namespace."
if [[ "$needs_storage_recovery" == true ]]; then
  [[ "$(kubectl auth can-i delete statefulsets.apps --namespace="$namespace")" == yes ]] || fail "The selected context cannot safely recreate the PostgreSQL StatefulSet."
  if [[ "$recreate_pvc" == true ]]; then
    [[ "$(kubectl auth can-i delete persistentvolumeclaims --namespace="$namespace")" == yes ]] || fail "The selected context cannot safely recreate the unbound PostgreSQL PVC."
  fi
fi
if [[ "$namespace_exists" == false ]]; then
  [[ "$(kubectl auth can-i create namespaces)" == yes ]] || fail "The selected context cannot create namespace $namespace."
fi

printf 'Migration preflight passed: every rendered SQL copy path matches a repository migration file.\n'
printf 'Using mutable :dev image tags; this path does not pin digests or verify image provenance.\n'

kubectl proxy --address=127.0.0.1 --port=0 > "$work_dir/kubectl-proxy.log" 2>&1 &
api_proxy_pid=$!
for attempt in {1..30}; do
  kill -0 "$api_proxy_pid" 2>/dev/null || fail "The loopback Kubernetes API proxy exited before becoming ready."
  api_port="$(awk '/Starting to serve on 127\.0\.0\.1:/ { sub(/^.*127\.0\.0\.1:/, ""); print; exit }' "$work_dir/kubectl-proxy.log")"
  if [[ -n "$api_port" ]] && curl -fsS --max-time 2 "http://127.0.0.1:$api_port/version" -o /dev/null; then
    break
  fi
  sleep 1
done
[[ -n "$api_port" ]] || fail "Timed out waiting for the loopback Kubernetes API proxy."

if [[ "$namespace_exists" == false ]]; then
  kubectl apply -f "$namespace_file"
fi

lock_holder="deploy-$$-$RANDOM"
lock_json="$(kubectl create -f - -o json <<EOF
apiVersion: coordination.k8s.io/v1
kind: Lease
metadata:
  name: $lock_name
  namespace: $namespace
  labels:
    app.kubernetes.io/part-of: codefactory
spec:
  holderIdentity: $lock_holder
EOF
)" || fail "The deployment Lease already exists or could not be acquired; no Secrets or workload resources were changed. Inspect it before retrying."
lock_uid="$(jq -r '.metadata.uid // empty' <<< "$lock_json")"
[[ -n "$lock_uid" ]] || fail "The created deployment Lease did not return a UID; stop and inspect namespace $namespace."

job_json="$(kubectl -n "$namespace" get job "$migration_job" -o json --ignore-not-found)" || fail "Unable to inspect the existing migration Job while holding the deployment Lease."
job_state=absent
job_uid=""
if [[ -n "$job_json" ]]; then
  if jq -e '(.status.active // 0) > 0' >/dev/null <<< "$job_json"; then
    fail "Migration Job $migration_job is active. It will not be deleted or replaced."
  elif jq -e '[.status.conditions[]? | select((.type == "Complete" or .type == "Failed") and .status == "True")] | length > 0' >/dev/null <<< "$job_json"; then
    job_state=terminal
    job_uid="$(jq -r '.metadata.uid // empty' <<< "$job_json")"
    [[ -n "$job_uid" ]] || fail "Terminal migration Job $migration_job has no UID; it will not be deleted."
  else
    fail "Migration Job $migration_job is not terminal. It will not be deleted or replaced."
  fi
fi

bash "$secrets_script"
delete_and_wait_for_uid() {
  local resource_url="$1"
  local resource_description="$2"
  local resource_uid="$3"
  local file_prefix="$4"
  local resource_version="$5"
  local delete_status
  local get_status
  local current_uid
  local delete_options
  [[ -n "$resource_uid" && -n "$resource_version" ]] || fail "$resource_description is missing its UID or resourceVersion; it will not be deleted."
  delete_options="$(jq -nc --arg uid "$resource_uid" --arg resource_version "$resource_version" '{apiVersion:"v1",kind:"DeleteOptions",propagationPolicy:"Foreground",preconditions:({uid:$uid,resourceVersion:$resource_version})}')"
  delete_status="$(curl -sS --max-time 15 -o "$work_dir/$file_prefix-delete.json" -w '%{http_code}' \
    -X DELETE -H 'Content-Type: application/json' \
    --data-binary "$delete_options" \
    "$resource_url")" || fail "Unable to submit UID-preconditioned deletion for $resource_description."
  [[ "$delete_status" == 200 || "$delete_status" == 202 ]] || fail "$resource_description deletion did not succeed with UID precondition (HTTP $delete_status)."
  for attempt in {1..60}; do
    get_status="$(curl -sS --max-time 5 -o "$work_dir/$file_prefix-current.json" -w '%{http_code}' "$resource_url")" || fail "Unable to verify deletion of $resource_description."
    if [[ "$get_status" == 404 ]]; then
      return
    fi
    [[ "$get_status" == 200 ]] || fail "Unexpected HTTP $get_status while verifying deletion of $resource_description."
    current_uid="$(jq -r '.metadata.uid // empty' "$work_dir/$file_prefix-current.json")"
    [[ "$current_uid" == "$resource_uid" ]] || fail "$resource_description name now belongs to a different UID; refusing to continue."
    sleep 1
  done
  fail "Timed out waiting for the UID-checked deletion of $resource_description."
}

if [[ "$needs_storage_recovery" == true ]]; then
  current_statefulset_json="$(kubectl -n "$namespace" get statefulset "$postgres_statefulset" -o json --ignore-not-found)" || fail "Unable to recheck the PostgreSQL StatefulSet before recovery."
  current_pvc_json="$(kubectl -n "$namespace" get pvc "$postgres_pvc" -o json --ignore-not-found)" || fail "Unable to recheck the PostgreSQL PVC before recovery."
  [[ "$(jq -r '.metadata.uid // empty' <<< "$current_statefulset_json")" == "$existing_statefulset_uid" ]] || fail "PostgreSQL StatefulSet identity changed after preflight; no storage objects will be deleted."
  [[ "$(jq -r '.metadata.uid // empty' <<< "$current_pvc_json")" == "$existing_pvc_uid" ]] || fail "PostgreSQL PVC identity changed after preflight; no storage objects will be deleted."
  postgres_storage_identity_is_recoverable "$current_pvc_json" "$current_statefulset_json" || fail "PostgreSQL PVC is no longer Pending and unbound or the StatefulSet/PVC identity changed; no storage objects will be deleted."
  [[ "$(jq -r '.spec.storageClassName // ""' <<< "$current_pvc_json")" == "$existing_storage_class" ]] || fail "PostgreSQL PVC storage class changed after preflight; no storage objects will be deleted."
  current_template_class="$(jq -r '.spec.volumeClaimTemplates[] | select(.metadata.name == "postgres-data") | .spec.storageClassName // ""' <<< "$current_statefulset_json")"
  [[ "$current_template_class" == "$existing_template_class" ]] || fail "PostgreSQL StatefulSet claim template changed after preflight; no storage objects will be deleted."
  current_statefulset_resource_version="$(jq -r '.metadata.resourceVersion // empty' <<< "$current_statefulset_json")"

  delete_and_wait_for_uid \
    "http://127.0.0.1:$api_port/apis/apps/v1/namespaces/$namespace/statefulsets/$postgres_statefulset" \
    "StatefulSet $postgres_statefulset" "$existing_statefulset_uid" statefulset "$current_statefulset_resource_version"
  if [[ "$recreate_pvc" == true ]]; then
    current_pvc_json="$(kubectl -n "$namespace" get pvc "$postgres_pvc" -o json --ignore-not-found)" || fail "Unable to recheck the PostgreSQL PVC before its deletion."
    [[ "$(jq -r '.metadata.uid // empty' <<< "$current_pvc_json")" == "$existing_pvc_uid" ]] || fail "PostgreSQL PVC identity changed after StatefulSet deletion; the PVC will not be deleted."
    jq -e --arg namespace "$namespace" --arg pvc "$postgres_pvc" '
      .metadata.name == $pvc and .metadata.namespace == $namespace and
      .metadata.uid != null and (.metadata.uid | length > 0) and
      .metadata.labels["app.kubernetes.io/name"] == "codefactory-postgres" and
      ((.metadata.ownerReferences // []) | length == 0) and
      .status.phase == "Pending" and (.spec.volumeName // "") == "" and
      (.spec.storageClassName // "") == "" and
      .spec.accessModes == ["ReadWriteOnce"] and
      .spec.resources.requests.storage == "10Gi"
    ' <<< "$current_pvc_json" >/dev/null || fail "PostgreSQL PVC is no longer the expected Pending, unbound legacy claim; it will not be deleted."
    current_pvc_resource_version="$(jq -r '.metadata.resourceVersion // empty' <<< "$current_pvc_json")"
    delete_and_wait_for_uid \
      "http://127.0.0.1:$api_port/api/v1/namespaces/$namespace/persistentvolumeclaims/$postgres_pvc" \
      "PVC $postgres_pvc" "$existing_pvc_uid" pvc "$current_pvc_resource_version"
  fi
  printf 'Removed only the UID-verified PostgreSQL StatefulSet%s; the dev render will recreate it.\n' \
    "$(if [[ "$recreate_pvc" == true ]]; then printf ' and Pending, unbound PVC'; else printf ' while preserving the existing PVC'; fi)"
fi

if [[ "$job_state" == terminal ]]; then
  job_url="http://127.0.0.1:$api_port/apis/batch/v1/namespaces/$namespace/jobs/$migration_job"
  delete_status="$(curl -sS --max-time 15 -o "$work_dir/job-delete.json" -w '%{http_code}' \
    -X DELETE -H 'Content-Type: application/json' \
    --data-binary "{\"apiVersion\":\"v1\",\"kind\":\"DeleteOptions\",\"propagationPolicy\":\"Foreground\",\"preconditions\":{\"uid\":\"$job_uid\"}}" \
    "$job_url")" || fail "Unable to submit UID-preconditioned deletion for migration Job $migration_job."
  [[ "$delete_status" == 200 || "$delete_status" == 202 ]] || fail "Migration Job deletion did not succeed with UID precondition (HTTP $delete_status); no replacement Job will be applied."
  job_deleted=false
  for attempt in {1..60}; do
    get_status="$(curl -sS --max-time 5 -o "$work_dir/job-current.json" -w '%{http_code}' "$job_url")" || fail "Unable to verify deletion of migration Job $migration_job."
    if [[ "$get_status" == 404 ]]; then
      job_deleted=true
      break
    fi
    [[ "$get_status" == 200 ]] || fail "Unexpected HTTP $get_status while verifying migration Job deletion."
    current_job_uid="$(jq -r '.metadata.uid // empty' "$work_dir/job-current.json")"
    [[ "$current_job_uid" == "$job_uid" ]] || fail "Migration Job name now belongs to a different UID; refusing to apply over the replacement."
    sleep 1
  done
  [[ "$job_deleted" == true ]] || fail "Timed out waiting for the UID-checked migration Job deletion."
fi
kubectl apply -f "$rendered_manifest"
kubectl -n "$namespace" rollout restart deployment/github-service deployment/langgraph-service deployment/management-portal

kubectl -n "$namespace" wait --for=jsonpath='{.status.phase}'=Bound "pvc/$postgres_pvc" --timeout=600s
kubectl -n "$namespace" rollout status "statefulset/$postgres_statefulset" --timeout=600s
kubectl -n "$namespace" wait --for=condition=complete "job/$migration_job" --timeout=960s
kubectl -n "$namespace" rollout status deployment/github-service --timeout=600s
kubectl -n "$namespace" rollout status deployment/langgraph-service --timeout=600s
kubectl -n "$namespace" rollout status deployment/management-portal --timeout=600s

start_port_forward() {
  local service="$1"
  local remote_port="$2"
  local log_file="$work_dir/$service.port-forward.log"
  local local_port=""
  kubectl -n "$namespace" port-forward --address 127.0.0.1 "service/$service" ":$remote_port" > "$log_file" 2>&1 &
  port_forward_pid=$!
  for attempt in {1..60}; do
    kill -0 "$port_forward_pid" 2>/dev/null || fail "Port-forward for $service exited before becoming ready."
    local_port="$(awk '/^Forwarding from 127\.0\.0\.1:/ { sub(/^Forwarding from 127\.0\.0\.1:/, ""); sub(/ .*/, ""); print; exit }' "$log_file")"
    if [[ -n "$local_port" ]] && curl -fsS --max-time 2 "http://127.0.0.1:$local_port/healthz" -o /dev/null; then
      printf 'Health check passed: %s /healthz\n' "$service"
      return
    fi
    sleep 1
  done
  fail "Timed out waiting for $service /healthz through its localhost-only port-forward."
}

stop_port_forward() {
  if [[ -n "$port_forward_pid" ]]; then
    kill "$port_forward_pid" >/dev/null 2>&1 || true
    wait "$port_forward_pid" >/dev/null 2>&1 || true
    port_forward_pid=""
  fi
}

start_port_forward github-service 8000
stop_port_forward
start_port_forward langgraph-service 8001
stop_port_forward

printf 'Dev deployment and smoke checks completed.\n'