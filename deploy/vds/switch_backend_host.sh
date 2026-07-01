#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  ./deploy/vds/switch_backend_host.sh <backend-host>

Example:
  ./deploy/vds/switch_backend_host.sh api.taksklad.uz

Run this script on the VDS from /opt/stacks/taksklad/app after DNS A records already point
to the server IP. The script updates deploy/vds/.env and recreates Traefik-routed
containers so new host rules are applied.
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

backend_host="${1:-}"

if [[ -z "$backend_host" ]]; then
  usage >&2
  exit 2
fi

app_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
env_file="$app_dir/deploy/vds/.env"
compose_file="$app_dir/deploy/vds/docker-compose.yml"

if [[ ! -f "$env_file" ]]; then
  echo "Missing $env_file" >&2
  exit 1
fi

if ! getent hosts "$backend_host" >/dev/null; then
  echo "DNS is not ready for $backend_host. Add the A record first, then rerun." >&2
  exit 1
fi

tmp_file="$(mktemp)"
awk -v backend="$backend_host" '
  BEGIN {
    backend_done = 0
  }
  /^TAKSKLAD_BACKEND_HOST=/ {
    print "TAKSKLAD_BACKEND_HOST=" backend
    backend_done = 1
    next
  }
  { print }
  END {
    if (!backend_done) {
      print "TAKSKLAD_BACKEND_HOST=" backend
    }
  }
' "$env_file" > "$tmp_file"
install -m 600 "$tmp_file" "$env_file"
rm -f "$tmp_file"

cd "$app_dir"
docker compose --env-file "$env_file" -f "$compose_file" up -d --force-recreate backend-api

echo "Backend host switched to $backend_host"
echo "Waiting for https://$backend_host/health"

for attempt in $(seq 1 45); do
  if curl -fsS "https://$backend_host/health" >/dev/null 2>&1; then
    echo "Health check OK: https://$backend_host/health"
    exit 0
  fi
  sleep 2
done

echo "Health check failed after waiting. Inspect Traefik/backend logs on the VDS." >&2
exit 1
