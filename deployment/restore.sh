#!/usr/bin/env bash
###############################################################################
# OpenReviewer 数据库恢复入口
#
# 用法：openreviewer-restore --restore /opt/openreviewer/backups/<backup>.dump
# 脚本先把备份恢复到临时数据库验证，再保留当前生产库为回退库并原子换名；当前
# release 的迁移和就绪检查全部成功后才删除回退库。失败时自动换回原生产库。
###############################################################################

set -Eeuo pipefail
umask 077

BASE_DIR="${OPENREVIEWER_BASE_DIR:-/opt/openreviewer}"
BACKUPS_DIR="$BASE_DIR/backups"
RESTORES_DIR="$BASE_DIR/restores"
CURRENT_LINK="$BASE_DIR/current"
LOCK_FILE="$BASE_DIR/deploy.lock"

die() {
  printf 'OpenReviewer restore failed: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

for command_name in awk basename chmod curl date docker flock id mkdir mv readlink \
  rm sha256sum sleep tail timeout tr wc; do
  require_command "$command_name"
done
docker compose version >/dev/null 2>&1 || die "docker compose plugin is unavailable"
[[ "$(id -u)" == "0" ]] || die "the restore entrypoint must run as root"
[[ "$#" == 2 && "$1" == "--restore" ]] || \
  die "usage: openreviewer-restore --restore <verified-backup.dump>"
[[ -d "$BACKUPS_DIR" && -L "$CURRENT_LINK" ]] || die "deployment directories are incomplete"

backups_real="$(readlink -f -- "$BACKUPS_DIR")"
backup_file="$(readlink -f -- "$2" 2>/dev/null || true)"
[[ -n "$backup_file" && -f "$backup_file" ]] || die "backup file does not exist"
[[ "$backup_file" == "$backups_real/"* ]] || die "backup must be inside the managed backup directory"
BACKUPS_DIR="$backups_real"
backup_name="$(basename -- "$backup_file")"
[[ "$backup_name" =~ ^[0-9]{8}T[0-9]{6}Z-([0-9a-f]{40}|pre-restore)\.dump$ ]] || \
  die "backup filename is not managed by OpenReviewer"
[[ -f "${backup_file}.sha256" ]] || die "backup checksum file is missing"
expected_checksum="$(awk -v expected_name="$backup_name" '
  { line_count += 1 }
  NF == 2 && length($1) == 64 && $1 !~ /[^0-9a-f]/ && $2 == expected_name {
    checksum = $1
    match_count += 1
  }
  END {
    if (line_count != 1 || match_count != 1) exit 1
    print checksum
  }
' "${backup_file}.sha256")" || die "backup checksum file is invalid"
actual_checksum="$(sha256sum "$backup_file" | awk '{print $1}')"
[[ "$actual_checksum" == "$expected_checksum" ]] || die "backup checksum does not match"

current_release="$(readlink -f -- "$CURRENT_LINK")"
[[ -f "$current_release/.env" && -f "$current_release/compose.yml" ]] || \
  die "current release is incomplete"
validate_env_file() {
  # Compose 对重复键采用最后一个值；恢复入口拒绝歧义配置，保证校验和实际
  # 启动使用同一组变量。
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

validate_env_file "$current_release/.env" || \
  die "current release .env contains invalid or duplicate keys"

compose_cmd=(docker compose --project-directory "$current_release" \
  --env-file "$current_release/.env" --file "$current_release/compose.yml")
"${compose_cmd[@]}" config --quiet

env_value() {
  local env_file="$1"
  local key="$2"
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

read_worker_replicas() {
  local env_file="$1"
  local replicas
  replicas="$(env_value "$env_file" OPENREVIEWER_WORKER_REPLICAS 2>/dev/null || printf '1')"
  [[ "$replicas" =~ ^[0-9]+$ ]] || return 1
  (( replicas >= 1 && replicas <= 20 )) || return 1
  printf '%s' "$replicas"
}

prune_emergency_backups() {
  # 恢复成功后只清理旧的 pre-restore 备份；请求中使用的备份始终保留。
  # 候选路径必须是 backups 根目录下的固定命名文件，拒绝符号链接和越界路径。
  local retention_count="$1"
  local protected_file="$2"
  local candidate candidate_name kept=0
  while IFS= read -r candidate; do
    candidate_name="${candidate#"$BACKUPS_DIR/"}"
    if [[ "$candidate" != "$BACKUPS_DIR/$candidate_name" || \
      ! "$candidate_name" =~ ^[0-9]{8}T[0-9]{6}Z-pre-restore\.dump$ || \
      ! -f "$candidate" || -L "$candidate" ]]; then
      printf 'emergency backup cleanup skipped: unexpected backup path\n' >&2
      return 1
    fi
    [[ "$candidate" == "$protected_file" ]] && continue
    if (( kept < retention_count )); then
      kept=$((kept + 1))
      continue
    fi
    if ! rm -f -- "$candidate" "${candidate}.sha256"; then
      printf 'emergency backup cleanup skipped: cannot remove %s\n' "$candidate_name" >&2
      return 1
    fi
  done < <(
    find "$BACKUPS_DIR" -maxdepth 1 -type f \
      -name '????????T??????Z-pre-restore.dump' \
      -printf '%T@ %p\n' \
      | LC_ALL=C sort -rn \
      | awk '{ sub(/^[^ ]+ /, ""); print }'
  )
}

worker_replicas="$(read_worker_replicas "$current_release/.env")" || \
  die "invalid worker replica count"

container_health() {
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' \
    "$1" 2>/dev/null || true
}

wait_healthy() {
  local container_name="$1"
  local timeout_seconds="${2:-180}"
  local started_at="$SECONDS"
  local state
  while (( SECONDS - started_at < timeout_seconds )); do
    state="$(container_health "$container_name")"
    [[ "$state" == "healthy" ]] && return 0
    [[ "$state" == "unhealthy" || "$state" == "no-healthcheck" ]] && return 1
    sleep 5
  done
  return 1
}

wait_compose_service_healthy() {
  local service_name="$1"
  local timeout_seconds="${2:-180}"
  local started_at="$SECONDS"
  local container_id remaining
  local -a container_ids

  while (( SECONDS - started_at < timeout_seconds )); do
    mapfile -t container_ids < <("${compose_cmd[@]}" ps --all --quiet "$service_name")
    if (( ${#container_ids[@]} > 0 )); then
      for container_id in "${container_ids[@]}"; do
        remaining=$((timeout_seconds - (SECONDS - started_at)))
        (( remaining > 0 )) || return 1
        wait_healthy "$container_id" "$remaining" || return 1
      done
      return 0
    fi
    sleep 2
  done
  printf 'timed out waiting for compose service: %s\n' "$service_name" >&2
  return 1
}

remove_stale_migration_container() {
  local state
  state="$(docker inspect --format '{{.State.Status}}' openreviewer-migrate 2>/dev/null || true)"
  case "$state" in
    "") ;;
    created|dead|exited) docker rm openreviewer-migrate >/dev/null ;;
    *) die "migration container is still active: $state" ;;
  esac
}

terminate_database_connections() {
  local database_name="$1"
  docker exec openreviewer-postgres psql --username openreviewer --dbname postgres \
    --no-align --tuples-only --set=ON_ERROR_STOP=1 \
    --command "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${database_name}' AND pid <> pg_backend_pid();" \
    >/dev/null
}

restore_timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
staging_database="openreviewer_restore_${restore_timestamp//[TZ]/}_$$"
rollback_database="openreviewer_rollback_${restore_timestamp//[TZ]/}_$$"
emergency_name="${restore_timestamp}-pre-restore.dump"
emergency_tmp="$BACKUPS_DIR/.${emergency_name}.tmp.$$"
emergency_checksum_tmp="$BACKUPS_DIR/.${emergency_name}.sha256.tmp.$$"
emergency_file="$BACKUPS_DIR/$emergency_name"
incomplete_emergency_file=""
incomplete_emergency_checksum=""
production_renamed=0
replacement_activated=0
staging_exists=0

cleanup() {
  set +e
  if (( staging_exists == 1 )); then
    docker exec openreviewer-postgres dropdb --username openreviewer --if-exists \
      --force "$staging_database" >/dev/null 2>&1
  fi
  rm -f -- "$emergency_tmp" "$emergency_checksum_tmp"
  if [[ -n "$incomplete_emergency_file" ]]; then
    rm -f -- "$incomplete_emergency_file"
  fi
  if [[ -n "$incomplete_emergency_checksum" ]]; then
    rm -f -- "$incomplete_emergency_checksum"
  fi
}
trap cleanup EXIT

application_services=(api worker web)
if "${compose_cmd[@]}" config --services | grep -Fxq index-worker; then
  application_services+=(index-worker)
fi

rollback_restore() {
  local status="$1"
  trap - ERR
  set +e
  if (( production_renamed == 1 )); then
    "${compose_cmd[@]}" stop "${application_services[@]}" >/dev/null 2>&1
    if (( replacement_activated == 1 )); then
      terminate_database_connections openreviewer
      docker exec openreviewer-postgres dropdb --username openreviewer --if-exists \
        --force openreviewer >/dev/null 2>&1
    fi
    docker exec openreviewer-postgres psql --username openreviewer --dbname postgres \
      --set=ON_ERROR_STOP=1 \
      --command "ALTER DATABASE \"${rollback_database}\" RENAME TO openreviewer;" \
      >/dev/null 2>&1
    "${compose_cmd[@]}" up -d --no-deps --scale "worker=$worker_replicas" \
      "${application_services[@]}" >/dev/null 2>&1
  fi
  printf 'restore failed; the original database was restored when possible\n' >&2
  exit "$status"
}
trap 'rollback_restore "$?"' ERR

exec 9>"$LOCK_FILE"
flock -w 300 9 || die "another deployment or restore is still running"
mkdir -p -- "$RESTORES_DIR"
chmod 700 "$RESTORES_DIR"

"${compose_cmd[@]}" up -d postgres
wait_healthy openreviewer-postgres 180 || die "PostgreSQL is not healthy"

# 第一次真实恢复只做预检，服务仍连接原生产库。
docker exec openreviewer-postgres createdb --username openreviewer \
  --template template0 "$staging_database"
staging_exists=1
timeout --signal=TERM 1800 docker exec -i openreviewer-postgres \
  pg_restore --username openreviewer --dbname "$staging_database" \
    --exit-on-error --no-owner --no-privileges < "$backup_file"
restored_revision="$(docker exec openreviewer-postgres psql --username openreviewer \
  --dbname "$staging_database" --no-align --tuples-only --set=ON_ERROR_STOP=1 \
  --command 'SELECT version_num FROM alembic_version;' | tr -d '[:space:]')"
[[ "$restored_revision" =~ ^[0-9]{8}_[0-9]{4}$ ]] || \
  die "restored database has no valid migration revision"

# 切换前再保存当前生产库；原数据库也会一直保留为回退库直到新库完全就绪。
[[ ! -e "$emergency_file" && ! -e "${emergency_file}.sha256" ]] || \
  die "emergency backup target already exists"
incomplete_emergency_file="$emergency_file"
incomplete_emergency_checksum="${emergency_file}.sha256"
timeout --signal=TERM 1800 docker exec openreviewer-postgres \
  pg_dump --username openreviewer --dbname openreviewer --format=custom \
    --compress=6 --no-owner --no-privileges > "$emergency_tmp"
[[ -s "$emergency_tmp" ]] || die "emergency backup is empty"
timeout --signal=TERM 120 docker exec -i openreviewer-postgres \
  pg_restore --list < "$emergency_tmp" >/dev/null
chmod 600 "$emergency_tmp"
mv -- "$emergency_tmp" "$emergency_file"
(
  cd "$BACKUPS_DIR"
  sha256sum "$emergency_name" > "$emergency_checksum_tmp"
)
chmod 600 "$emergency_checksum_tmp"
mv -- "$emergency_checksum_tmp" "${emergency_file}.sha256"
incomplete_emergency_file=""
incomplete_emergency_checksum=""

"${compose_cmd[@]}" stop "${application_services[@]}"
terminate_database_connections openreviewer
docker exec openreviewer-postgres psql --username openreviewer --dbname postgres \
  --set=ON_ERROR_STOP=1 \
  --command "ALTER DATABASE openreviewer RENAME TO \"${rollback_database}\";"
production_renamed=1
docker exec openreviewer-postgres psql --username openreviewer --dbname postgres \
  --set=ON_ERROR_STOP=1 \
  --command "ALTER DATABASE \"${staging_database}\" RENAME TO openreviewer;"
replacement_activated=1
staging_exists=0

remove_stale_migration_container
"${compose_cmd[@]}" run --rm --no-deps migrate
"${compose_cmd[@]}" up -d --no-deps --scale "worker=$worker_replicas" "${application_services[@]}"
wait_healthy openreviewer-api 180
wait_compose_service_healthy worker 180
if [[ " ${application_services[*]} " == *" index-worker "* ]]; then
  wait_compose_service_healthy index-worker 180
fi
wait_healthy openreviewer-web 180
api_host_port="$(env_value "$current_release/.env" OPENREVIEWER_API_HOST_PORT 2>/dev/null || printf '18090')"
[[ "$api_host_port" =~ ^[0-9]+$ ]] || die "invalid API host port"
curl --fail --silent --show-error --max-time 15 \
  "http://127.0.0.1:${api_host_port}/readyz" >/dev/null

terminate_database_connections "$rollback_database"
docker exec openreviewer-postgres dropdb --username openreviewer --if-exists \
  --force "$rollback_database"
production_renamed=0
replacement_activated=0
final_revision="$(docker exec openreviewer-api python -m alembic current | tail -n 1 | tr -d '\r')"
if ! prune_emergency_backups "$retention_count" "$backup_file"; then
  printf 'warning: emergency backup cleanup did not complete; restore remains active\n' >&2
fi
restore_info="$RESTORES_DIR/${restore_timestamp}.info"
{
  printf 'backup=%s\n' "$backup_name"
  printf 'backup_revision=%s\n' "$restored_revision"
  printf 'final_revision=%s\n' "$final_revision"
  printf 'emergency_backup=%s\n' "$emergency_name"
  printf 'restored_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$restore_info"
chmod 600 "$restore_info"
trap - ERR
printf 'database restored backup=%s migration=%s\n' "$backup_name" "$final_revision"
