#!/usr/bin/env bash
# Download and install one checksummed study package without pruning versions.
set -euo pipefail

# Deployment commands must not resolve from a caller-controlled PATH.
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
readonly PATH

readonly study_root=/srv/epi-agent/study_data
readonly -a study_python=(
  /usr/sbin/runuser
  --user epi-agent-web
  --
  /usr/bin/env
  -i
  PATH=/opt/epi-agent/current/.venv/bin:/usr/bin
  LANG=C.UTF-8
  LC_ALL=C.UTF-8
  PYTHONUTF8=1
  REPORT_AGENT_STUDY_ROOT=/srv/epi-agent/study_data
  /opt/epi-agent/current/.venv/bin/python
)

usage() {
  printf '%s\n' 'usage: install-study.sh <bucket> <study-key> <sha256> <study-id> <version>' >&2
  exit 64
}

fail() {
  printf 'install-study: %s\n' "$1" >&2
  exit 1
}

require_root() {
  [ "$(id -u)" -eq 0 ] || fail 'must be run as root'
}

is_safe_s3_key() {
  [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] && [[ $1 != *..* ]]
}

cleanup() {
  local exit_status=$?
  trap - EXIT
  if [ -n "${staging_dir:-}" ] && [ -d "$staging_dir" ]; then
    rm -rf -- "$staging_dir"
  fi
  exit "$exit_status"
}

require_root
[ "$#" -eq 5 ] || usage

bucket=$1
study_key=$2
study_sha256=$3
study_id=$4
package_version=$5
[[ $bucket =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || fail 'bucket is invalid'
is_safe_s3_key "$study_key" || fail 'study key is invalid'
[[ $study_sha256 =~ ^[0-9a-f]{64}$ ]] || fail 'study SHA-256 is invalid'
[[ $study_id =~ ^[a-z0-9][a-z0-9._-]*$ ]] || fail 'study ID is invalid'
[[ $package_version =~ ^[a-z0-9][a-z0-9._-]*$ ]] || fail 'study version is invalid'

install -d -m 0750 -o epi-agent-web -g epi-agent-web "$study_root"
staging_dir=$(mktemp -d /srv/epi-agent/study-staging.XXXXXX)
trap cleanup EXIT
archive_path="$staging_dir/study.tar.gz"

aws s3 cp "s3://$bucket/$study_key" "$archive_path"
printf '%s  %s\n' "$study_sha256" "$archive_path" | sha256sum -c -
chown -R -h epi-agent-web:epi-agent-web "$study_root" "$staging_dir"
"${study_python[@]}" /opt/epi-agent/current/study_installer.py \
  "--study" "$archive_path"

"${study_python[@]}" - "$study_root" "$study_id" "$package_version" <<'PY'
import json
from pathlib import Path
import sys

study_root = Path(sys.argv[1])
study_id = sys.argv[2]
package_version = sys.argv[3]
installed_manifest = (
    study_root / "studies" / "packages" / study_id / package_version / "study-package.json"
)
try:
    manifest = json.loads(installed_manifest.read_text(encoding="utf-8"))
except (OSError, ValueError) as error:
    raise SystemExit(f"installed study manifest is invalid: {error}") from error
if manifest.get("study_id") != study_id or manifest.get("package_version") != package_version:
    raise SystemExit("installed study manifest does not match requested ID/version")
PY
