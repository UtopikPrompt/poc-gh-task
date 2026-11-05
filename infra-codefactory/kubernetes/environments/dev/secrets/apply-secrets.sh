#!/usr/bin/env bash
set -euo pipefail

secrets_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
runtime_env="$secrets_dir/runtime-secrets.env"
dashboard_oauth_env="$secrets_dir/dashboard-oauth.env"
private_key_file="$secrets_dir/github-app.pem"
oauth_only=false

case "$#" in
  0) ;;
  1)
    if [[ "$1" == "--oauth-only" ]]; then
      oauth_only=true
    else
      printf 'Usage: %s [--oauth-only]\n' "${BASH_SOURCE[0]}" >&2
      exit 2
    fi
    ;;
  *)
    printf 'Usage: %s [--oauth-only]\n' "${BASH_SOURCE[0]}" >&2
    exit 2
    ;;
esac

if [[ "$oauth_only" != true ]]; then
  if [[ ! -f "$runtime_env" || ! -r "$runtime_env" ]]; then
    printf 'Copy runtime-secrets.env.example to runtime-secrets.env first.\n' >&2
    exit 1
  fi

  if [[ ! -f "$private_key_file" || ! -s "$private_key_file" || ! -r "$private_key_file" ]]; then
    printf 'The GitHub App private-key file github-app.pem is missing, empty, or unreadable.\n' >&2
    exit 1
  fi
fi

if [[ ! -f "$dashboard_oauth_env" || ! -r "$dashboard_oauth_env" ]]; then
  printf 'Copy dashboard-oauth.env.example to dashboard-oauth.env and fill its required entries first.\n' >&2
  exit 1
fi

command -v kubectl >/dev/null 2>&1 || {
  printf 'Required tool kubectl is not installed or not on PATH.\n' >&2
  exit 1
}

if [[ "$oauth_only" != true ]]; then
  if ! awk -F= '
    BEGIN {
      allowed["POSTGRES_ADMIN_PASSWORD"] = 1
      allowed["GITHUB_DB_PASSWORD"] = 1
      allowed["LANGGRAPH_DB_PASSWORD"] = 1
      allowed["GITHUB_WEBHOOK_SECRET"] = 1
      allowed["INTERNAL_SERVICE_TOKEN"] = 1
      allowed["MODEL_RECONCILER_TOKEN"] = 1
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

  for key in POSTGRES_ADMIN_PASSWORD GITHUB_DB_PASSWORD LANGGRAPH_DB_PASSWORD GITHUB_WEBHOOK_SECRET INTERNAL_SERVICE_TOKEN MODEL_RECONCILER_TOKEN; do
    if ! awk -F= -v key="$key" '$1 == key && length(substr($0, index($0, "=") + 1)) > 0 { found = 1 } END { exit !found }' "$runtime_env"; then
      printf 'runtime-secrets.env must define a non-empty %s entry.\n' "$key" >&2
      exit 1
    fi
  done
fi

if ! awk '
  BEGIN {
    allowed["GITHUB_OAUTH_CLIENT_ID"] = 1
    allowed["GITHUB_OAUTH_CLIENT_SECRET"] = 1
    allowed["DASHBOARD_SESSION_SIGNING_KEY"] = 1
  }
  /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
  {
    separator = index($0, "=")
    if (!separator) {
      print "dashboard-oauth.env contains an invalid entry; expected KEY=value." > "/dev/stderr"
      failed = 1
      next
    }
    key = substr($0, 1, separator - 1)
    value = substr($0, separator + 1)
    if (!(key in allowed)) {
      print "dashboard-oauth.env contains an unsupported key." > "/dev/stderr"
      failed = 1
      next
    }
    if (seen[key]++) {
      printf "dashboard-oauth.env contains duplicate key %s.\n", key > "/dev/stderr"
      failed = 1
      next
    }
    if (value !~ /[^[:space:]]/) {
      printf "dashboard-oauth.env must define a non-empty %s entry.\n", key > "/dev/stderr"
      failed = 1
    }
  }
  END {
    for (key in allowed) {
      if (!seen[key]) {
        printf "dashboard-oauth.env must define a non-empty %s entry.\n", key > "/dev/stderr"
        failed = 1
      }
    }
    exit failed
  }
' "$dashboard_oauth_env"; then
  exit 1
fi

printf 'Configure %s with your GitHub OAuth values. Set the callback URL to https://codefactory.utopiklab.com/auth; request scope read:org; allowed organization: UtopikPrompt.\n' \
  "$dashboard_oauth_env"

if [[ "$oauth_only" != true ]]; then
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
fi

kubectl create secret generic github-dashboard-oauth \
  --namespace codefactory \
  --from-env-file="$dashboard_oauth_env" \
  --dry-run=client -o yaml |
  kubectl apply -f -