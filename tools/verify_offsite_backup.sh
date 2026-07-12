#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_BUCKET=false
CHECKSUM=false

usage() {
  cat <<'EOF'
Usage: verify_offsite_backup.sh --test-bucket --checksum

The test bucket is a local, isolated simulation. No network request, credential,
production backup, or external object-store mutation is performed.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --test-bucket) TEST_BUCKET=true ;;
    --checksum) CHECKSUM=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ "$TEST_BUCKET" != true || "$CHECKSUM" != true ]]; then
  echo "Only the safe local contract --test-bucket --checksum is available" >&2
  exit 2
fi
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }

BACKUP_DIR="${TAKSKLAD_BACKUP_TEST_DIR:-$APP_DIR/test-artifacts/phase24/backups}"
BUCKET_DIR="${TAKSKLAD_OFFSITE_TEST_BUCKET_DIR:-$APP_DIR/test-artifacts/phase24/offsite-test-bucket}"
KEY_VAULT_DIR="${TAKSKLAD_OFFSITE_TEST_KEY_VAULT_DIR:-$APP_DIR/.release-state/phase24-key-vault}"
EVIDENCE_DIR="${TAKSKLAD_OFFSITE_EVIDENCE_DIR:-$APP_DIR/test-artifacts/phase24}"
mkdir -p "$BACKUP_DIR/completed" "$BUCKET_DIR" "$KEY_VAULT_DIR" "$EVIDENCE_DIR"
chmod 700 "$BACKUP_DIR" "$BACKUP_DIR/completed" "$BUCKET_DIR" "$KEY_VAULT_DIR"

latest_manifest="$(find "$BACKUP_DIR/completed" -mindepth 2 -maxdepth 2 -type f -name 'taksklad-postgres-*.manifest.json' -print | sort | tail -n 1)"
if [[ -z "$latest_manifest" ]]; then
  "$APP_DIR/deploy/vds/backup_postgres.sh" --test-mode --synthetic-db >/dev/null
  latest_manifest="$(find "$BACKUP_DIR/completed" -mindepth 2 -maxdepth 2 -type f -name 'taksklad-postgres-*.manifest.json' -print | sort | tail -n 1)"
fi
[[ -n "$latest_manifest" && -f "$latest_manifest" ]] || { echo "Synthetic backup manifest not found" >&2; exit 1; }

readarray_compat() {
  python3 - "$1" <<'PY'
import json
import os
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
if (
    payload.get("schema_version") != 2
    or payload.get("source") != "synthetic-postgresql"
    or payload.get("actual_postgresql") is not True
    or payload.get("contains_customer_content") is not False
    or payload.get("atomic_bundle") is not True
):
    raise SystemExit("refusing non-synthetic or customer-content backup")
archive = payload.get("archive") or {}
values = [
    payload.get("backup_id"),
    archive.get("filename"),
    archive.get("sha256"),
    archive.get("checksum_sidecar"),
]
if not all(isinstance(value, str) and value for value in values):
    raise SystemExit("invalid backup manifest")
backup_id, archive_name, digest, checksum_name = values
if not re.fullmatch(r"taksklad-postgres-[A-Za-z0-9-]+", backup_id):
    raise SystemExit("invalid backup ID")
if os.path.basename(archive_name) != archive_name or os.path.basename(checksum_name) != checksum_name:
    raise SystemExit("unsafe backup manifest filename")
if not re.fullmatch(r"[0-9a-f]{64}", digest):
    raise SystemExit("invalid archive checksum")
for value in values:
    print(value)
PY
}

manifest_values="$(readarray_compat "$latest_manifest")"
backup_id="$(printf '%s\n' "$manifest_values" | sed -n '1p')"
archive_name="$(printf '%s\n' "$manifest_values" | sed -n '2p')"
archive_sha256="$(printf '%s\n' "$manifest_values" | sed -n '3p')"
checksum_name="$(printf '%s\n' "$manifest_values" | sed -n '4p')"
bundle_dir="$(dirname "$latest_manifest")"
archive_file="$bundle_dir/$archive_name"
checksum_file="$bundle_dir/$checksum_name"
[[ -f "$archive_file" ]] || { echo "Synthetic archive missing: $archive_name" >&2; exit 1; }
[[ -f "$checksum_file" ]] || { echo "Checksum sidecar missing: $checksum_name" >&2; exit 1; }

actual_archive_sha256="$(shasum -a 256 "$archive_file" | awk '{print $1}')"
[[ "$actual_archive_sha256" == "$archive_sha256" ]] || { echo "Local archive checksum mismatch" >&2; exit 1; }
expected_sidecar="$archive_sha256  $archive_name"
[[ "$(cat "$checksum_file")" == "$expected_sidecar" ]] || { echo "Checksum sidecar mismatch" >&2; exit 1; }

key_file="$KEY_VAULT_DIR/$backup_id.recovery-key"
key_partial="$key_file.partial"
encrypted_staging="$(mktemp "${TMPDIR:-/tmp}/taksklad-offsite-encrypted.XXXXXX")"
encrypted_partial="$BUCKET_DIR/$archive_name.enc.partial"
encrypted_object="$BUCKET_DIR/$archive_name.enc"
decrypted_probe="$(mktemp "${TMPDIR:-/tmp}/taksklad-offsite-decrypt.XXXXXX")"
evidence_partial="$EVIDENCE_DIR/offsite-backup-evidence.json.partial"
evidence_file="$EVIDENCE_DIR/offsite-backup-evidence.json"
success=false

cleanup() {
  rm -f "$key_partial" "$encrypted_staging" "$decrypted_probe" "$encrypted_partial" "$evidence_partial"
  if [[ "$success" != true ]]; then
    rm -f "$key_file" "$encrypted_object"
  fi
}
trap cleanup EXIT
[[ ! -e "$key_file" && ! -e "$encrypted_object" ]] || { echo "Offsite test artifact already exists for backup ID" >&2; exit 1; }
chmod 600 "$encrypted_staging" "$decrypted_probe"
openssl rand -hex 32 >"$key_partial"
chmod 600 "$key_partial"
mv "$key_partial" "$key_file"
key_mode="$(stat -c '%a' "$key_file" 2>/dev/null || stat -f '%Lp' "$key_file")"
[[ "$key_mode" == "600" ]] || { echo "Recovery key permissions are not 0600" >&2; exit 1; }
openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
  -in "$archive_file" -out "$encrypted_staging" -pass "file:$key_file"
cp "$encrypted_staging" "$encrypted_partial"
chmod 600 "$encrypted_partial"
mv "$encrypted_partial" "$encrypted_object"

encrypted_sha256="$(shasum -a 256 "$encrypted_staging" | awk '{print $1}')"
copied_sha256="$(shasum -a 256 "$BUCKET_DIR/$(basename "$encrypted_object")" | awk '{print $1}')"
[[ "$encrypted_sha256" == "$copied_sha256" ]] || { echo "Offsite object checksum mismatch" >&2; exit 1; }

openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
  -in "$encrypted_object" -out "$decrypted_probe" -pass "file:$key_file"
decrypted_sha256="$(shasum -a 256 "$decrypted_probe" | awk '{print $1}')"
[[ "$decrypted_sha256" == "$archive_sha256" ]] || { echo "Encrypted object decrypt verification failed" >&2; exit 1; }

key_sha256="$(shasum -a 256 "$key_file" | awk '{print $1}')"
python3 - "$evidence_partial" "$backup_id" "$(basename "$encrypted_object")" \
  "$archive_sha256" "$encrypted_sha256" "$(basename "$key_file")" "$key_sha256" <<'PY'
import json
import os
import sys

path, backup_id, object_name, source_sha256, object_sha256, key_name, key_sha256 = sys.argv[1:]
payload = {
    "schema_version": 1,
    "mode": "local-test-bucket",
    "external_mutations": 0,
    "backup_id": backup_id,
    "object": {
        "name": object_name,
        "encrypted": True,
        "encryption": "AES-256-CBC-PBKDF2-200000",
        "sha256": object_sha256,
        "checksum_verified": True,
        "decrypt_verified_source_sha256": source_sha256,
    },
    "recovery_key": {
        "vault": "separate-local-simulated-key-vault",
        "name": key_name,
        "sha256": key_sha256,
        "mode": "0600",
        "retained": True,
    },
    "recoverable": True,
    "contains_customer_content": False,
}
with open(path, "x", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
PY
chmod 600 "$evidence_partial"
mv -f "$evidence_partial" "$evidence_file"
success=true
trap - EXIT
rm -f "$encrypted_staging" "$decrypted_probe"

printf 'OFFSITE_BACKUP_OK backup_id=%s encrypted_sha256=%s source_sha256=%s checksum=verified decrypt=verified recoverable=true key_mode=0600 external_mutations=0 evidence=%s\n' \
  "$backup_id" "$encrypted_sha256" "$archive_sha256" "$evidence_file"
