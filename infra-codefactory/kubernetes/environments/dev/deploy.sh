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

for tool in bash kubectl crane curl jq tar awk yq sleep mktemp; do
  command -v "$tool" >/dev/null 2>&1 || fail "Required tool '$tool' is not installed or not on PATH."
done
yq_version="$(yq --version 2>/dev/null)" || fail "Unable to identify yq; Mike Farah yq v4 is required."
[[ "$yq_version" == *'version v4.'* ]] || fail "Mike Farah yq v4 is required; found an unsupported yq implementation."
(( BASH_VERSINFO[0] >= 4 )) || fail "Bash 4 or newer is required."
work_dir="$(mktemp -d)" || fail "Unable to create a temporary work directory."

current_context="$(kubectl config current-context 2>/dev/null)" || fail "kubectl has no usable current context."
[[ -n "$current_context" ]] || fail "kubectl returned an empty current context."
printf 'Current kubectl context: %s\n' "$current_context"
[[ -n "${CODEFACTORY_DEV_CONTEXT:-}" ]] || fail "Set CODEFACTORY_DEV_CONTEXT to the exact dev context name before running this noninteractive deploy."
[[ "$current_context" == "$CODEFACTORY_DEV_CONTEXT" ]] || fail "Current context does not match CODEFACTORY_DEV_CONTEXT. No resources were changed."

rendered_manifest="$work_dir/dev.yaml"
kubectl kustomize "$overlay_dir" > "$rendered_manifest" || fail "Unable to render the dev overlay."

config_value() {
  local resource_name="$1"
  local key="$2"
  awk -v wanted_name="$resource_name" -v wanted_key="$key" '
    function process_document(    i, kind, name, prefix, value) {
      for (i = 1; i <= line_count; i++) {
        if (document[i] == "kind: ConfigMap") kind = 1
        if (document[i] == "  name: " wanted_name) name = 1
      }
      if (!kind || !name) return
      prefix = "  " wanted_key ":"
      for (i = 1; i <= line_count; i++) {
        if (index(document[i], prefix) != 1) continue
        value = substr(document[i], length(prefix) + 1)
        sub(/^[[:space:]]+/, "", value)
        sub(/[[:space:]]+#.*$/, "", value)
        sub(/^"/, "", value)
        sub(/"$/, "", value)
        print value
        found = 1
        return
      }
    }
    /^---$/ { process_document(); for (i in document) delete document[i]; line_count = 0; if (found) exit; next }
    { document[++line_count] = $0 }
    END { if (!found) process_document() }
  ' "$rendered_manifest"
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

mapfile -t rendered_images < <(yq -r '.. | select(tag == "!!map" and has("image")) | .image | select(tag == "!!str")' "$rendered_manifest")
github_tag_ref=""
langgraph_tag_ref=""
for image_ref in "${rendered_images[@]}"; do
  case "$image_ref" in
    ghcr.io/utopikprompt/poc-gh-task-github-service:*)
      [[ -z "$github_tag_ref" || "$github_tag_ref" == "$image_ref" ]] || fail "The rendered dev overlay contains multiple GitHub service image tags."
      github_tag_ref="$image_ref"
      ;;
    ghcr.io/utopikprompt/poc-gh-task-langgraph-service:*)
      [[ -z "$langgraph_tag_ref" || "$langgraph_tag_ref" == "$image_ref" ]] || fail "The rendered dev overlay contains multiple LangGraph service image tags."
      langgraph_tag_ref="$image_ref"
      ;;
  esac
done
[[ -n "$github_tag_ref" ]] || fail "The rendered dev overlay is missing the GitHub service image."
[[ -n "$langgraph_tag_ref" ]] || fail "The rendered dev overlay is missing the LangGraph service image."

resolve_digest() {
  local image_tag="$1"
  local digest
  digest="$(crane digest "$image_tag")" || fail "Unable to resolve public image tag $image_tag to a digest."
  [[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]] || fail "Registry returned an invalid image digest for $image_tag."
  printf '%s@%s\n' "${image_tag%:*}" "$digest"
}
github_digest_ref="$(resolve_digest "$github_tag_ref")"
langgraph_digest_ref="$(resolve_digest "$langgraph_tag_ref")"

GITHUB_TAG_REF="$github_tag_ref" GITHUB_DIGEST_REF="$github_digest_ref" \
  yq -i '(... | select(tag == "!!map" and .image == strenv(GITHUB_TAG_REF)).image) = strenv(GITHUB_DIGEST_REF)' "$rendered_manifest" \
  || fail "Unable to pin GitHub service image references in the rendered manifest."
LANGGRAPH_TAG_REF="$langgraph_tag_ref" LANGGRAPH_DIGEST_REF="$langgraph_digest_ref" \
  yq -i '(... | select(tag == "!!map" and .image == strenv(LANGGRAPH_TAG_REF)).image) = strenv(LANGGRAPH_DIGEST_REF)' "$rendered_manifest" \
  || fail "Unable to pin LangGraph service image references in the rendered manifest."

mapfile -t pinned_images < <(yq -r '.. | select(tag == "!!map" and has("image")) | .image | select(tag == "!!str")' "$rendered_manifest")
github_pinned_count=0
langgraph_pinned_count=0
for image_ref in "${pinned_images[@]}"; do
  case "$image_ref" in
    ghcr.io/utopikprompt/poc-gh-task-github-service*)
      [[ "$image_ref" == "$github_digest_ref" ]] || fail "A rendered GitHub service image was not pinned to the inspected digest."
      ((github_pinned_count += 1))
      ;;
    ghcr.io/utopikprompt/poc-gh-task-langgraph-service*)
      [[ "$image_ref" == "$langgraph_digest_ref" ]] || fail "A rendered LangGraph service image was not pinned to the inspected digest."
      ((langgraph_pinned_count += 1))
      ;;
  esac
done
(( github_pinned_count > 0 && langgraph_pinned_count > 0 )) || fail "The rendered manifest does not contain both pinned service images."

migration_manifest="$repo_root/infra-codefactory/kubernetes/bases/platform/database-migrations.yaml"
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
      [[ " ${github_migrations[*]} " == *" $migration_path "* ]] || github_migrations+=("$migration_path")
    else
      [[ " ${langgraph_migrations[*]} " == *" $migration_path "* ]] || langgraph_migrations+=("$migration_path")
    fi
    args="${args#*"/app/migrations/$migration_file"}"
  done
done < <(yq -r '.spec.template.spec.initContainers[] | [.name, (.args | join(" "))] | @tsv' "$migration_manifest")
[[ "$github_init_seen" == true && "${#github_migrations[@]}" -gt 0 ]] || fail "Unable to derive GitHub migration SQL paths from database-migrations.yaml."
[[ "$langgraph_init_seen" == true && "${#langgraph_migrations[@]}" -gt 0 ]] || fail "Unable to derive LangGraph migration SQL paths from database-migrations.yaml."

check_migration_artifacts() {
  local image="$1"
  local archive="$2"
  shift 2
  crane export "$image" - > "$archive" || fail "Unable to inspect public image digest $image."
  local migration_path
  for migration_path in "$@"; do
    if ! tar -tf "$archive" | awk -v expected="$migration_path" '$0 == expected { found = 1 } END { exit !found }'; then
      fail "Public image digest $image lacks $migration_path. Publish updated dev service images with the repository's Build and publish service images workflow, then retry."
    fi
  done
}
check_migration_artifacts "$github_digest_ref" "$work_dir/github-image.tar" "${github_migrations[@]}"
check_migration_artifacts "$langgraph_digest_ref" "$work_dir/langgraph-image.tar" "${langgraph_migrations[@]}"

storage_class=codefactory-ssd
mapfile -t rendered_storage_classes < <(yq -r 'select(.kind == "StorageClass" and .metadata.name == "codefactory-ssd") | .metadata.name' "$rendered_manifest")
mapfile -t rendered_postgres_volumes < <(yq -r 'select(.kind == "PersistentVolume" and .metadata.name == "codefactory-postgres-data") | .metadata.name' "$rendered_manifest")
[[ "${#rendered_storage_classes[@]}" == 1 && "${rendered_storage_classes[0]}" == "$storage_class" ]] || fail "The dev overlay must render exactly one StorageClass named $storage_class."
[[ "${#rendered_postgres_volumes[@]}" == 1 ]] || fail "The dev overlay must render exactly one PersistentVolume named codefactory-postgres-data."
rendered_storage_class_json="$(yq -o=json 'select(.kind == "StorageClass" and .metadata.name == "codefactory-ssd")' "$rendered_manifest")" || fail "Unable to read the rendered PostgreSQL StorageClass."
rendered_postgres_volume_json="$(yq -o=json 'select(.kind == "PersistentVolume" and .metadata.name == "codefactory-postgres-data")' "$rendered_manifest")" || fail "Unable to read the rendered PostgreSQL PersistentVolume."
rendered_claim_class="$(yq -r 'select(.kind == "StatefulSet" and .metadata.name == "codefactory-postgres") | .spec.volumeClaimTemplates[] | select(.metadata.name == "postgres-data") | .spec.storageClassName // ""' "$rendered_manifest")" || fail "Unable to read the rendered PostgreSQL claim template."
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
      needs_storage_recovery=true
      recreate_pvc=true
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

printf 'Digest-pinned migration artifact preflight passed for both rendered dev services.\n'

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

kubectl -n "$namespace" wait --for=jsonpath='{.status.phase}'=Bound "pvc/$postgres_pvc" --timeout=600s
kubectl -n "$namespace" rollout status "statefulset/$postgres_statefulset" --timeout=600s
kubectl -n "$namespace" wait --for=condition=complete "job/$migration_job" --timeout=960s
kubectl -n "$namespace" rollout status deployment/github-service --timeout=600s
kubectl -n "$namespace" rollout status deployment/langgraph-service --timeout=600s

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