#!/usr/bin/env bash
# Install an immutable, checksummed Epi-Agent release. This script is intended
# to be called by a root-owned deployment mechanism with positional arguments.
set -Eeuo pipefail

# Deployment commands must not resolve from a caller-controlled PATH.
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
readonly PATH

readonly releases_root=/opt/epi-agent/releases
readonly current_link=/opt/epi-agent/current
readonly maintenance_file=/run/epi-agent/maintenance
readonly drain_deadline_seconds=600

usage() {
  printf '%s\n' 'usage: install-release.sh <bucket> <release-key> <sha256> <release-id> <domain> <certificate-email> [--force]' >&2
  exit 64
}

fail() {
  printf 'install-release: %s\n' "$1" >&2
  exit 1
}

require_root() {
  [ "$(id -u)" -eq 0 ] || fail 'must be run as root'
}

is_safe_s3_key() {
  [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] && [[ $1 != *..* ]]
}

is_safe_domain() {
  [[ $1 =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$ ]]
}

is_safe_email() {
  [[ $1 =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]]
}

cleanup() {
  local exit_status=$?
  trap - EXIT ERR
  set +e
  if [ "${activated:-false}" = true ]; then
    restore_previous_release
  fi
  rm -f -- "$maintenance_file"
  if [ -n "${staging_dir:-}" ] && [ -d "$staging_dir" ]; then
    rm -rf -- "$staging_dir"
  fi
  exit "$exit_status"
}

restore_previous_release() {
  set +e
  if [ "${previous_current_exists:-false}" = true ]; then
    rm -f -- /opt/epi-agent/current.rollback
    ln -s "$previous_target" /opt/epi-agent/current.rollback
    mv -Tf /opt/epi-agent/current.rollback /opt/epi-agent/current
    systemctl restart epi-agent.service
  else
    rm -f -- "$current_link"
    systemctl stop epi-agent.service
  fi
}

wait_for_drain() {
  local response_file
  response_file="$staging_dir/deployment-status.json"
  while :; do
    if curl --fail --silent --show-error --max-time 5 \
      http://127.0.0.1:8000/api/ops/deployment-status >"$response_file" \
      && /usr/bin/python3.12 - "$response_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    status = json.load(handle)
if status.get("maintenance") is True and status.get("active_runs") == 0:
    raise SystemExit(0)
raise SystemExit(1)
PY
    then
      return 0
    fi
    if [ "$SECONDS" -ge "$drain_deadline_seconds" ]; then
      rm -f -- "$maintenance_file"
      fail 'timed out waiting for active runs to drain; release was not switched'
    fi
    sleep 2
  done
}

validate_and_extract_archive() {
  /usr/bin/python3.12 - "$archive_path" "$release_payload" "$release_id" <<'PY'
import json
from pathlib import Path, PurePosixPath
import sys
import tarfile

archive_path = Path(sys.argv[1])
destination = Path(sys.argv[2])
release_id = sys.argv[3]
with tarfile.open(archive_path, "r:gz") as archive:
    members = archive.getmembers()
    seen = set()
    for member in members:
        path = PurePosixPath(member.name)
        if (
            not member.name
            or member.name.startswith("/")
            or "\\" in member.name
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or member.name in seen
            or member.issym()
            or member.islnk()
            or not member.isfile()
        ):
            raise SystemExit(f"unsafe tar member: {member.name!r}")
        seen.add(member.name)
    archive.extractall(destination, members=members, filter="data")

manifest_path = destination / "release.json"
try:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
except (OSError, ValueError) as error:
    raise SystemExit(f"invalid release manifest: {error}") from error
if manifest.get("commit_sha") != release_id or manifest.get("python_version") != "3.12":
    raise SystemExit("release manifest does not match the requested release")
PY
}

check_service() {
  curl --fail --silent --show-error --max-time 15 http://127.0.0.1:8000/api/health >/dev/null
  curl --fail --silent --show-error --max-time 15 http://127.0.0.1:8000/api/readiness \
    | /usr/bin/python3.12 -c 'import json, sys; raise SystemExit(0 if json.load(sys.stdin).get("status") == "ready" else 1)'
}

require_root
[ "$#" -eq 6 ] || [ "$#" -eq 7 ] || usage

bucket=$1
release_key=$2
release_sha256=$3
release_id=$4
domain=$5
certificate_email=$6
force=false
if [ "$#" -eq 7 ]; then
  [ "$7" = --force ] || usage
  force=true
fi

[[ $bucket =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || fail 'bucket is invalid'
is_safe_s3_key "$release_key" || fail 'release key is invalid'
[[ $release_id =~ ^[0-9a-f]{40}$ ]] || fail 'release ID is invalid'
[[ $release_sha256 =~ ^[0-9a-f]{64}$ ]] || fail 'release SHA-256 is invalid'
is_safe_domain "$domain" || fail 'domain is invalid'
is_safe_email "$certificate_email" || fail 'certificate email is invalid'

trap cleanup EXIT
trap 'exit $?' ERR

install -d -m 0755 /opt/epi-agent "$releases_root"
install -d -m 0750 -o root -g epi-agent-web /run/epi-agent
install -m 0640 -o root -g epi-agent-web /dev/null "$maintenance_file"

staging_dir=$(mktemp -d /opt/epi-agent/staging.XXXXXX)
archive_path="$staging_dir/release.tar.gz"
release_payload="$staging_dir/release"
mkdir -m 0755 "$release_payload"

if [ -L "$current_link" ]; then
  wait_for_drain
fi
aws s3 cp "s3://$bucket/$release_key" "$archive_path"
printf '%s  %s\n' "$release_sha256" "$archive_path" | sha256sum -c -
validate_and_extract_archive

release_dir="$releases_root/$release_id"
if [ -e "$release_dir" ]; then
  if [ "$force" != true ] && { [ ! -L "$current_link" ] || [ "$(readlink -- "$current_link")" != "$release_dir" ]; }; then
    fail 'release already exists; use --force only to activate a non-current release'
  fi
else
  venv_python="$release_payload/.venv/bin/python"
  (
    cd "$release_payload"
    uv venv --python 3.12 .venv
    uv pip install --python "$venv_python" --requirement requirements.txt
  )
  mv -T "$release_payload" "$release_dir"
fi

previous_current_exists=false
previous_target=
if [ -L "$current_link" ]; then
  previous_current_exists=true
  previous_target=$(readlink -- "$current_link")
elif [ -e "$current_link" ]; then
  fail 'current release path must be a symlink'
fi

rm -f -- /opt/epi-agent/current.next
ln -s "$release_dir" /opt/epi-agent/current.next
mv -Tf /opt/epi-agent/current.next /opt/epi-agent/current
activated=true
systemctl restart epi-agent.service
curl --fail --silent --show-error --max-time 15 http://127.0.0.1:8000/api/health >/dev/null
rm -f -- "$maintenance_file"
check_service
activated=false

if [ ! -e "/etc/letsencrypt/live/$domain/fullchain.pem" ]; then
  /usr/bin/certbot certonly --webroot -w /var/www/certbot --non-interactive --agree-tos \
    --email "$certificate_email" -d "$domain"
fi

readonly staged_nginx_config=/etc/nginx/staged/epi-agent.conf
readonly live_nginx_config=/etc/nginx/conf.d/epi-agent.conf
readonly bootstrap_nginx_config=/etc/nginx/conf.d/epi-agent-bootstrap.conf
restore_nginx_config() {
  rm -f -- "$live_nginx_config" "$bootstrap_nginx_config"
  [ -f "$nginx_backup/live" ] && mv -Tf "$nginx_backup/live" "$live_nginx_config"
  [ -f "$nginx_backup/bootstrap" ] && mv -Tf "$nginx_backup/bootstrap" "$bootstrap_nginx_config"
  nginx -t && systemctl reload nginx.service || true
}
activate_staged_nginx_config() {
  nginx_backup=$(mktemp -d /etc/nginx/.epi-agent-backup.XXXXXX)
  [ -f "$live_nginx_config" ] && cp -p -- "$live_nginx_config" "$nginx_backup/live"
  [ -f "$bootstrap_nginx_config" ] && cp -p -- "$bootstrap_nginx_config" "$nginx_backup/bootstrap"
  mv -Tf "$staged_nginx_config" "$live_nginx_config"
  rm -f -- "$bootstrap_nginx_config"
  if nginx -t && systemctl reload nginx.service; then
    rm -rf -- "$nginx_backup"
    return 0
  fi
  restore_nginx_config
  return 1
}
if [ -f "$staged_nginx_config" ]; then
  install -d -m 0755 /etc/nginx/conf.d
  activate_staged_nginx_config
fi
