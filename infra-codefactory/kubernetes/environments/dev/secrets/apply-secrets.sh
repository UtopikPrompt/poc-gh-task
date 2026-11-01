#!/usr/bin/env bash
set -euo pipefail

secrets_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
runtime_env="$secrets_dir/runtime-secrets.env"
private_key_file="$secrets_dir/github-app.pem"

if [[ ! -r "$runtime_env" ]]; then
  printf 'Copy runtime-secrets.env.example to runtime-secrets.env first.\n' >&2
  exit 1
fi

if [[ ! -s "$private_key_file" ]]; then
  printf 'The GitHub App private-key file github-app.pem is missing or empty.\n' >&2
  exit 1
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
  !($1 in allowed) {
    printf "runtime-secrets.env contains unsupported key %s; non-secret database settings belong in ConfigMap config.\n", $1
    failed = 1
  }
  END { exit failed }
' "$runtime_env"; then
  exit 1
fi

for key in POSTGRES_ADMIN_PASSWORD GITHUB_DB_PASSWORD LANGGRAPH_DB_PASSWORD GITHUB_WEBHOOK_SECRET INTERNAL_SERVICE_TOKEN; do
  if ! awk -F= -v key="$key" '$1 == key && length(substr($0, index($0, "=") + 1)) > 0 { found = 1 } END { exit !found }' "$runtime_env"; then
    printf 'runtime-secrets.env must define a non-empty %s entry.\n' "$key" >&2
    exit 1
  fi
done

kubectl create secret generic codefactory-runtime-secrets \
  --namespace codefactory \
  --from-env-file="$runtime_env" \
  --dry-run=client -o yaml |
  kubectl apply -f -

kubectl create secret generic codefactory-github-app \
  --namespace codefactory \
  --from-file="private-key.pem=$private_key_file" \
  --dry-run=client -o yaml |
  kubectl apply -f -