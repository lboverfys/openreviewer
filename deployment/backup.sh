#!/usr/bin/env bash
###############################################################################
# OpenReviewer 定时数据库备份入口
#
# 每次备份都执行 custom-format 导出、pg_restore 清单检查、临时数据库真实恢复、
# Alembic 版本检查和 SHA-256 校验。可选的 root 管理镜像命令用于把最终文件复制到
# 异地存储；脚本只向它传递备份和校验文件的绝对路径，不执行环境变量中的 shell。
###############################################################################

set -Eeuo pipefail
umask 077

BASE_DIR="${OPENREVIEWER_BASE_DIR:-/opt/openreviewer}"
BACKUPS_DIR="$BASE_DIR/backups"
CURRENT_LINK="$BASE_DIR/current"
LOCK_FILE="$BASE_DIR/deploy.lock"

die() {
  printf 'OpenReviewer backup failed: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

for command_name in awk basename chmod date docker find flock id mkdir mv readlink \
  rm sha256sum sleep sort stat timeout tr; do
  require_command "$command_name"
done
docker compose version >/dev/null 2>&1 || die "docker compose plugin is unavailable"
[[ "$(id -u)" == "0" ]] || die "the backup entrypoint must run as root"
[[ -L "$CURRENT_LINK" ]] || die "current release link is missing"

current_release="$(readlink -f -- "$CURRENT_LINK")"
env_file="$current_release/.env"
compose_file="$current_release/compose.yml"
[[ -f "$env_file" && -f "$compose_file" ]] || die "current release is incomplete"

validate_env_file() {
  # 拒绝重复键，避免备份脚本读取的值与 Compose 的最后一个值不一致。
  local target_env_file="$1"
  awk '
    /^[[:space:]]*$/ || /^[[:space:]]*#/ { next }
    {
      line = $0
      sub(/^[[:space:]]*/, "", line)
      separator = index(line, "=")
      if (separator <= 1) { invalid = 1; next }
      key = substr(line, 1, separator - 1)
      sub(/[[:space:]]+$/, "", key)
      if (key !~ /^[A-Za-z_][A-Za-z0-9_]*$/) { invalid = 1; next }
      if (++seen[key] > 1) duplicate = 1
    }
    END { status = (invalid || duplicate) ? 1 : 0; exit status }
  ' "$target_env_file"
}

validate_env_file "$env_file" || die "current release .env contains invalid or duplicate keys"

env_value() {
  local key="$1"
  awk -v expected_key="$key" '
    /^[[:space:]]*$/ || /^[[:space:]]*#/ { next }
    {
      line = $0
      sub(/^[[:space:]]*/, "", line)
      separator = index(line, "=")
      if (separator <= 1) next
      candidate = substr(line, 1, separator - 1)
      sub(/[[:space:]]+$/, "", candidate)
      if (candidate == expected_key) { value = substr(line, separator + 1); matches += 1 }
    }
    END { if (matches != 1) exit 1; print value }
  ' "$env_file"
}

release_value() {
  local key="$1"
  local release_info="$current_release/release.info"
  [[ -f "$release_info" ]] || return 1
  awk -F= -v key="$key" '
    $1 == key { print substr($0, index($0, "=") + 1); found = 1; exit }
    END { if (!found) exit 1 }
  ' "$release_info"
}

retention_count="$(env_value OPENREVIEWER_BACKUP_RETENTION_COUNT 2>/dev/null || printf '14')"
[[ "$retention_count" =~ ^[0-9]+$ ]] || die "invalid database backup retention count"
(( retention_count >= 3 && retention_count <= 100 )) || \
  die "database backup retention count must be between 3 and 100"

mirror_required="$(env_value OPENREVIEWER_BACKUP_REQUIRE_MIRROR 2>/dev/null || printf 'false')"
[[ "$mirror_required" == "true" || "$mirror_required" == "false" ]] || \
  die "OPENREVIEWER_BACKUP_REQUIRE_MIRROR must be true or false"
mirror_command="$(env_value OPENREVIEWER_BACKUP_MIRROR_COMMAND 2>/dev/null || true)"

validate_mirror_command() {
  [[ -n "$mirror_command" ]] || {
    [[ "$mirror_required" == "false" ]] || die "required backup mirror command is missing"
    return 0
  }
  [[ "$mirror_command" =~ ^/usr/local/libexec/[A-Za-z0-9._/-]+$ ]] || \
    die "backup mirror command must be under /usr/local/libexec"
  local resolved_command
  resolved_command="$(readlink -f -- "$mirror_command" 2>/dev/null || true)"
  [[ -n "$resolved_command" && "$resolved_command" == "$mirror_command" ]] || \
    die "backup mirror command must use its canonical path"
  [[ -f "$mirror_command" && -x "$mirror_command" && ! -L "$mirror_command" ]] || \
    die "backup mirror command is not a regular executable"
  local owner mode
  owner="$(stat -c '%u' "$mirror_command")"
  mode="$(stat -c '%a' "$mirror_command")"
  [[ "$owner" == "0" && "$mode" =~ ^[0-7]{3,4}$ ]] || \
    die "backup mirror command ownership or mode is invalid"
  (( (8#$mode & 022) == 0 )) || die "backup mirror command is group/world writable"
}

validate_mirror_command
mkdir -p -- "$BACKUPS_DIR"
chmod 700 "$BACKUPS_DIR"

exec 9>"$LOCK_FILE"
flock -w 300 9 || die "another deployment, backup, or restore is still running"

compose_cmd=(docker compose --project-directory "$current_release" \
  --env-file "$env_file" --file "$compose_file")
"${compose_cmd[@]}" config --quiet
"${compose_cmd[@]}" up -d postgres

started_at="$SECONDS"
while (( SECONDS - started_at < 180 )); do
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' \
    openreviewer-postgres 2>/dev/null || true)"
  [[ "$health" == "healthy" ]] && break
  [[ "$health" == "unhealthy" || "$health" == "no-healthcheck" ]] && \
    die "PostgreSQL is not healthy"
  sleep 5
done
[[ "${health:-}" == "healthy" ]] || die "timed out waiting for PostgreSQL"

commit_sha="$(release_value commit 2>/dev/null || basename -- "$current_release")"
[[ "$commit_sha" =~ ^[0-9a-f]{40}$ ]] || die "current release has no valid commit SHA"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_name="${timestamp}-${commit_sha}.dump"
backup_file="$BACKUPS_DIR/$backup_name"
checksum_file="${backup_file}.sha256"
temporary_backup="$BACKUPS_DIR/.${backup_name}.tmp.$$"
temporary_checksum="$BACKUPS_DIR/.${backup_name}.sha256.tmp.$$"
restore_database="openreviewer_backup_verify_${timestamp//[TZ]/}_$$"
restore_exists=0

cleanup() {
  set +e
  if (( restore_exists == 1 )); then
    docker exec openreviewer-postgres dropdb --username openreviewer --if-exists \
      --force "$restore_database" >/dev/null 2>&1
  fi
  rm -f -- "$temporary_backup" "$temporary_checksum"
}
trap cleanup EXIT

[[ ! -e "$backup_file" && ! -e "$checksum_file" ]] || die "backup target already exists"
timeout --signal=TERM 1800 docker exec openreviewer-postgres \
  pg_dump --username openreviewer --dbname openreviewer --format=custom \
    --compress=6 --no-owner --no-privileges > "$temporary_backup"
[[ -s "$temporary_backup" ]] || die "database backup is empty"
chmod 600 "$temporary_backup"
timeout --signal=TERM 120 docker exec -i openreviewer-postgres \
  pg_restore --list < "$temporary_backup" >/dev/null

docker exec openreviewer-postgres createdb --username openreviewer \
  --template template0 "$restore_database"
restore_exists=1
timeout --signal=TERM 1800 docker exec -i openreviewer-postgres \
  pg_restore --username openreviewer --dbname "$restore_database" \
    --exit-on-error --no-owner --no-privileges < "$temporary_backup"
table_count="$(docker exec openreviewer-postgres psql --username openreviewer \
  --dbname "$restore_database" --no-align --tuples-only --set=ON_ERROR_STOP=1 \
  --command "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname = 'public';" \
  | tr -d '[:space:]')"
revision="$(docker exec openreviewer-postgres psql --username openreviewer \
  --dbname "$restore_database" --no-align --tuples-only --set=ON_ERROR_STOP=1 \
  --command 'SELECT version_num FROM alembic_version;' | tr -d '[:space:]')"
[[ "$table_count" =~ ^[1-9][0-9]*$ ]] || die "restored backup contains no public tables"
[[ "$revision" =~ ^[0-9]{8}_[0-9]{4}$ ]] || die "restored backup has no valid migration revision"
docker exec openreviewer-postgres dropdb --username openreviewer --if-exists \
  --force "$restore_database"
restore_exists=0

checksum="$(sha256sum "$temporary_backup" | awk '{print $1}')"
[[ "$checksum" =~ ^[0-9a-f]{64}$ ]] || die "database backup checksum failed"
printf '%s  %s\n' "$checksum" "$backup_name" > "$temporary_checksum"
chmod 600 "$temporary_checksum"
mv -- "$temporary_backup" "$backup_file"
mv -- "$temporary_checksum" "$checksum_file"

if [[ -n "$mirror_command" ]]; then
  timeout --signal=TERM 1800 "$mirror_command" "$backup_file" "$checksum_file" || \
    die "offsite backup mirror failed; local verified backup was retained"
fi

while IFS= read -r candidate; do
  candidate_name="${candidate#"$BACKUPS_DIR/"}"
  [[ "$candidate" == "$BACKUPS_DIR/$candidate_name" && \
    "$candidate_name" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{40}\.dump$ ]] || \
    die "refusing to remove unexpected backup path"
  rm -f -- "$candidate" "${candidate}.sha256"
done < <(
  find "$BACKUPS_DIR" -maxdepth 1 -type f \
    -name '????????T??????Z-????????????????????????????????????????.dump' \
    -printf '%T@ %p\n' \
    | LC_ALL=C sort -rn \
    | awk -v keep="$retention_count" 'NR > keep { sub(/^[^ ]+ /, ""); print }'
)

trap - EXIT
printf 'verified database backup=%s revision=%s mirrored=%s\n' \
  "$backup_name" "$revision" "$([[ -n "$mirror_command" ]] && printf 'true' || printf 'false')"
