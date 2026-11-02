#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/../../../.." && pwd)"
cd "$repo_root"

bash -n "$script_dir/deploy.sh"
if grep -Eq '(^|[^[:alnum:]_-])(crane|yq)([^[:alnum:]_-]|$)' "$script_dir/deploy.sh"; then
  printf 'The dev deploy script still references crane or yq.\n' >&2
  exit 1
fi

rendered_json="$(kubectl kustomize "$script_dir" | kubectl create --dry-run=client -f - -o json | jq -s '.')"
jq -e '
  def containers: (.spec.template.spec // {}) | ((.initContainers // []) + (.containers // []));
  [ .[] | select(.kind == "Deployment" and .metadata.name == "github-service") | containers[] | select(.name == "github-service" and .image == "ghcr.io/utopikprompt/poc-gh-task-github-service:dev" and .imagePullPolicy == "Always") ] | length == 1
' <<< "$rendered_json" >/dev/null
jq -e '
  def containers: (.spec.template.spec // {}) | ((.initContainers // []) + (.containers // []));
  [ .[] | select((.kind == "Deployment" and .metadata.name == "langgraph-service") or (.kind == "Job" and .metadata.name == "codefactory-database-migrations")) | containers[] | select(.image == "ghcr.io/utopikprompt/poc-gh-task-langgraph-service:dev" and .imagePullPolicy == "Always") ] | length == 3
' <<< "$rendered_json" >/dev/null
jq -e '
  [ .[] | select(.kind == "Job" and .metadata.name == "codefactory-database-migrations") | .spec.template.spec.initContainers[] | select(.name == "copy-github-migrations" and .image == "ghcr.io/utopikprompt/poc-gh-task-github-service:dev" and .imagePullPolicy == "Always") ] | length == 1
' <<< "$rendered_json" >/dev/null
jq -e '
  [ .[] | select(.kind == "Deployment" and .metadata.name == "management-portal") | .spec.template.spec.containers[] | select(.image == "ghcr.io/utopikprompt/poc-gh-task-management-portal:dev" and .imagePullPolicy == "Always") ] | length == 1
' <<< "$rendered_json" >/dev/null

check_migration_set() {
  local init_name="$1"
  local source_dir="$2"
  local -a rendered_paths=()
  local -a source_paths=()
  local source_file

  mapfile -t rendered_paths < <(
    jq -r --arg init_name "$init_name" '
      .[] | select(.kind == "Job" and .metadata.name == "codefactory-database-migrations") |
      .spec.template.spec.initContainers[] | select(.name == $init_name) | .args[] |
      scan("/app/migrations/([A-Za-z0-9_.-]+\\.sql)") | "app/migrations/" + .[0]
    ' <<< "$rendered_json" | sort
  )
  for source_file in "$source_dir"/*.sql; do
    [[ -f "$source_file" ]] && source_paths+=("app/migrations/${source_file##*/}")
  done
  mapfile -t source_paths < <(printf '%s\n' "${source_paths[@]}" | sort)

  [[ "${#rendered_paths[@]}" -gt 0 && "${#rendered_paths[@]}" == "${#source_paths[@]}" ]] || {
    printf 'Migration copy list does not match %s.\n' "$source_dir" >&2
    return 1
  }
  local index
  for index in "${!source_paths[@]}"; do
    [[ "${rendered_paths[$index]}" == "${source_paths[$index]}" ]] || {
      printf 'Migration copy list does not match %s.\n' "$source_dir" >&2
      return 1
    }
  done
}

check_migration_set copy-github-migrations "$repo_root/codefactory/github-service/migrations"
check_migration_set copy-langgraph-migration "$repo_root/codefactory/langgraph-service/migrations"
printf 'Dev deployment contract passed: mutable :dev refs, Always pulls, and complete migration copy lists.\n'