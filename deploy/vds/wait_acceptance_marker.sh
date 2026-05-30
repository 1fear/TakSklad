#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERIFY_SCRIPT="$SCRIPT_DIR/verify_acceptance_marker.sh"

usage() {
  cat >&2 <<'EOF'
Usage:
  wait_acceptance_marker.sh MARKER [--timeout SECONDS] [--interval SECONDS] [--expect-orders N] [--expect-scans N] [--expect-completed]

Wait until the read-only acceptance verifier passes.

Safety:
  MARKER must contain ACCEPTANCE, WEB_UI_SMOKE, or SMOKE_MVP.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi

MARKER="$1"
shift
TIMEOUT_SECONDS=300
INTERVAL_SECONDS=10
VERIFY_ARGS=("$MARKER")

while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout)
      if [[ $# -lt 2 || ! "$2" =~ ^[0-9]+$ ]]; then
        usage
        exit 2
      fi
      TIMEOUT_SECONDS="$2"
      shift 2
      ;;
    --interval)
      if [[ $# -lt 2 || ! "$2" =~ ^[0-9]+$ ]]; then
        usage
        exit 2
      fi
      INTERVAL_SECONDS="$2"
      shift 2
      ;;
    --expect-orders)
      if [[ $# -lt 2 || ! "$2" =~ ^[0-9]+$ ]]; then
        usage
        exit 2
      fi
      VERIFY_ARGS+=("--expect-orders" "$2")
      shift 2
      ;;
    --expect-scans)
      if [[ $# -lt 2 || ! "$2" =~ ^[0-9]+$ ]]; then
        usage
        exit 2
      fi
      VERIFY_ARGS+=("--expect-scans" "$2")
      shift 2
      ;;
    --expect-completed)
      VERIFY_ARGS+=("--expect-completed")
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if [[ ! -x "$VERIFY_SCRIPT" ]]; then
  echo "Verifier is not executable: $VERIFY_SCRIPT" >&2
  exit 1
fi
if [[ "$TIMEOUT_SECONDS" -lt 1 || "$INTERVAL_SECONDS" -lt 1 ]]; then
  usage
  exit 2
fi

START_TS="$(date +%s)"
ATTEMPT=1
LAST_OUTPUT=""
LAST_STATUS=0

while true; do
  set +e
  OUTPUT="$("$VERIFY_SCRIPT" "${VERIFY_ARGS[@]}" 2>&1)"
  STATUS="$?"
  set -e

  if [[ "$STATUS" -eq 0 ]]; then
    echo "$OUTPUT"
    echo "Acceptance marker is ready after attempt $ATTEMPT."
    exit 0
  fi

  LAST_OUTPUT="$OUTPUT"
  LAST_STATUS="$STATUS"
  if [[ "$STATUS" -eq 2 ]]; then
    echo "$LAST_OUTPUT" >&2
    exit "$STATUS"
  fi
  NOW_TS="$(date +%s)"
  ELAPSED=$((NOW_TS - START_TS))
  if [[ "$ELAPSED" -ge "$TIMEOUT_SECONDS" ]]; then
    echo "Timed out after ${ELAPSED}s waiting for acceptance marker." >&2
    echo "Last verifier exit code: $LAST_STATUS" >&2
    echo "$LAST_OUTPUT" >&2
    exit "$LAST_STATUS"
  fi

  echo "Attempt $ATTEMPT failed with exit $STATUS; retrying in ${INTERVAL_SECONDS}s..."
  sleep "$INTERVAL_SECONDS"
  ATTEMPT=$((ATTEMPT + 1))
done
