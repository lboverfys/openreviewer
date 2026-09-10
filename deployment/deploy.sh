#!/usr/bin/env bash
###############################################################################
# OpenReviewer niuma-2 发布入口
#
# GitHub Actions 通过受限 SSH 密钥执行本脚本。脚本从标准输入接收本次提交
# deployment 目录中固定白名单文件组成的 tar 包（含 CI 生成的镜像和运维脚本 digest），从
# 当前发布复制服务器专有的 .env，只替换两个应用镜像标签，然后执行迁移、滚动
# 启动和健康检查。
#
# 设计约束：
# 1. 每个提交使用独立的不可变 releases/<sha> 目录；current 只在新版本健康后
#    原子切换，因此失败版本不会覆盖上一个可用版本。
# 2. 数据库迁移只向前执行。回滚应用容器时不执行 alembic downgrade，避免
#    自动破坏已经升级的数据结构。
# 3. 脚本不读取或打印 .env 的具体内容，也不把密码、会话密钥写入 release.info。
# 4. 发布前核对服务器上部署、备份、恢复入口的 SHA-256，避免运维脚本版本漂移。
###############################################################################

set -Eeuo pipefail
umask 077

BASE_DIR="${OPENREVIEWER_BASE_DIR:-/opt/openreviewer}"
RELEASES_DIR="$BASE_DIR/releases"
BACKUPS_DIR="$BASE_DIR/backups"
CURRENT_LINK="$BASE_DIR/current"
LOCK_FILE="$BASE_DIR/deploy.lock"
EXPECTED_API_IMAGE_PREFIX="ghcr.io/lboverfys/openreviewer:"
EXPECTED_WEB_IMAGE_PREFIX="ghcr.io/lboverfys/openreviewer-web:"
BACKUP_HELPER_PATH="/usr/local/libexec/openreviewer-backup"
RESTORE_HELPER_PATH="/usr/local/libexec/openreviewer-restore"
RELEASE_BUNDLE_FILES=(
  compose.yml
  image-digests.env
  helper-digests.env
  prometheus-alerts.yml
  observability/alertmanager.yml
  observability/alertmanager-noop.yml
  observability/alert-webhook-url.placeholder
  observability/grafana-dashboard.json
  observability/grafana-dashboards.yml
  observability/grafana-datasource.yml
  observability/prometheus.yml
)

die() {
  printf 'OpenReviewer deployment failed: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

for command_name in awk basename cat chmod cmp cp curl date docker find flock grep id ln mkdir mktemp mv \
  readlink rm sed sha256sum sleep sort tail tar timeout tr wc; do
  require_command "$command_name"
done

docker compose version >/dev/null 2>&1 || die "docker compose plugin is unavailable"

if [[ "$(id -u)" != "0" ]]; then
  die "the deployment entrypoint must run as root"
fi

###############################################################################
# 受限 SSH key 的 forced command 没有把 SHA 作为 argv 传入，而是把原始命令放在
# SSH_ORIGINAL_COMMAND；手工本机执行时则允许显式传一个 SHA，方便首次安装和排障。
###############################################################################
commit_sha="${1:-}"
if [[ -n "${SSH_ORIGINAL_COMMAND:-}" ]]; then
  if [[ "${SSH_ORIGINAL_COMMAND}" =~ ^openreviewer-deploy[[:space:]]([0-9a-f]{40})$ ]]; then
    commit_sha="${BASH_REMATCH[1]}"
  else
    die "unsupported SSH command"
  fi
fi

if [[ ! "$commit_sha" =~ ^[0-9a-f]{40}$ ]]; then
  die "usage: openreviewer-deploy <40-character commit SHA>"
fi

[[ -d "$BASE_DIR" ]] || die "deployment base directory does not exist: $BASE_DIR"
[[ -d "$RELEASES_DIR" ]] || die "release directory does not exist: $RELEASES_DIR"
[[ -L "$CURRENT_LINK" ]] || die "current release link is missing or is not a symlink"

receive_release_bundle() {
  local target_dir="$1"
  local archive_file="$target_dir/release.tar"
  local actual_members expected_members relative_path
  [[ -t 0 ]] && die "release bundle must be supplied through SSH stdin"
  if ! timeout --signal=TERM 30 cat > "$archive_file"; then
    die "timed out while receiving release bundle"
  fi
  [[ -s "$archive_file" ]] || die "received release bundle is empty"
  [[ "$(wc -c < "$archive_file")" -le 1048576 ]] || \
    die "received release bundle is too large"

  actual_members="$(tar --list --file "$archive_file" | LC_ALL=C sort)" || \
    die "release bundle is not a readable tar archive"
  expected_members="$(printf '%s\n' "${RELEASE_BUNDLE_FILES[@]}" | LC_ALL=C sort)"
  [[ "$actual_members" == "$expected_members" ]] || \
    die "release bundle contains unexpected or missing files"
  tar --list --verbose --file "$archive_file" \
    | awk 'substr($1, 1, 1) != "-" { exit 1 } END { if (NR == 0) exit 1 }' || \
    die "release bundle contains a non-regular member"

  tar --extract --file "$archive_file" --directory "$target_dir" \
    --no-same-owner --no-same-permissions
  rm -f -- "$archive_file"
  for relative_path in "${RELEASE_BUNDLE_FILES[@]}"; do
    [[ -f "$target_dir/$relative_path" && ! -L "$target_dir/$relative_path" ]] || \
      die "release bundle member is not a regular file: $relative_path"
    chmod 644 "$target_dir/$relative_path"
  done
}

validate_env_file() {
  # Compose 对重复键采用最后一个值，而简单的 shell 读取通常采用第一个值。
  # 发布前拒绝重复/畸形赋值，避免脚本校验的镜像与 Compose 实际使用的镜像不一致。
  local env_file="$1"
  awk '
    /^[[:space:]]*$/ || /^[[:space:]]*#/ { next }
    {
      line = $0
      sub(/^[[:space:]]*/, "", line)
      separator = index(line, "=")
      if (separator <= 1) {
        invalid = 1
        next
      }
      key = substr(line, 1, separator - 1)
      sub(/[[:space:]]+$/, "", key)
      if (key !~ /^[A-Za-z_][A-Za-z0-9_]*$/) {
        invalid = 1
        next
      }
      if (++seen[key] > 1) duplicate = 1
    }
    END {
      status = (invalid || duplicate) ? 1 : 0
      exit status
    }
  ' "$env_file"
}

env_value() {
  # 只读取非敏感的发布字段；不会把服务器密码或会话密钥打印到日志。
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
      if (candidate == expected_key) {
        value = substr(line, separator + 1)
        matches += 1
      }
    }
    END {
      if (matches != 1) exit 1
      print value
    }
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

read_deploy_stop_timeout() {
  local env_file="$1"
  local timeout_seconds
  timeout_seconds="$(env_value "$env_file" OPENREVIEWER_DEPLOY_STOP_TIMEOUT_SECONDS 2>/dev/null || printf '960')"
  [[ "$timeout_seconds" =~ ^[0-9]+$ ]] || return 1
  (( timeout_seconds >= 30 && timeout_seconds <= 3600 )) || return 1
  printf '%s' "$timeout_seconds"
}

assert_env_value() {
  local env_file="$1"
  local key="$2"
  local expected="$3"
  local actual
  actual="$(env_value "$env_file" "$key" 2>/dev/null || true)"
  [[ "$actual" == "$expected" ]] || die "${key} does not match this commit"
}

read_helper_digest() {
  local manifest_file="$1"
  local expected_key="$2"
  awk -F= -v expected_key="$expected_key" '
    BEGIN { valid = 1; matches = 0 }
    /^[[:space:]]*$/ || /^#/ { next }
    NF != 2 { valid = 0; next }
    $1 !~ /^OPENREVIEWER_(DEPLOY|BACKUP|RESTORE)_HELPER_SHA256$/ {
      valid = 0
      next
    }
    $2 !~ /^[0-9a-f]{64}$/ { valid = 0; next }
    $1 == expected_key {
      value = $2
      matches += 1
    }
    END {
      if (!valid || matches != 1) exit 1
      print value
    }
  ' "$manifest_file"
}

assert_helper_digest() {
  local manifest_file="$1"
  local expected_key="$2"
  local helper_path="$3"
  local expected actual
  [[ -f "$helper_path" && ! -L "$helper_path" ]] || \
    die "installed helper is missing or not a regular file: $helper_path"
  expected="$(read_helper_digest "$manifest_file" "$expected_key" 2>/dev/null || true)"
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]] || \
    die "helper digest manifest is invalid: $expected_key"
  actual="$(sha256sum "$helper_path" | awk '{print $1}')"
  [[ "$actual" == "$expected" ]] || \
    die "installed helper version drift: $helper_path"
}

verify_installed_helpers() {
  local manifest_file="$1"
  local deploy_path
  deploy_path="$(readlink -f -- "${BASH_SOURCE[0]}" 2>/dev/null || true)"
  [[ -n "$deploy_path" ]] || die "cannot resolve deployment helper path"
  assert_helper_digest "$manifest_file" \
    OPENREVIEWER_DEPLOY_HELPER_SHA256 "$deploy_path"
  assert_helper_digest "$manifest_file" \
    OPENREVIEWER_BACKUP_HELPER_SHA256 "$BACKUP_HELPER_PATH"
  assert_helper_digest "$manifest_file" \
    OPENREVIEWER_RESTORE_HELPER_SHA256 "$RESTORE_HELPER_PATH"
}

read_image_digest() {
  local manifest_file="$1"
  local expected_key="$2"
  awk -F= -v expected_key="$expected_key" '
    BEGIN { valid = 1; matches = 0 }
    /^[[:space:]]*$/ || /^#/ { next }
    NF != 2 { valid = 0; next }
    $1 !~ /^OPENREVIEWER_(API|WEB)_IMAGE_DIGEST$/ { valid = 0; next }
    $2 !~ /^sha256:[0-9a-f]{64}$/ { valid = 0; next }
    $1 == expected_key { value = $2; matches += 1 }
    END {
      if (!valid || matches != 1) exit 1
      print value
    }
  ' "$manifest_file"
}

assert_image_digest() {
  local image="$1"
  local repository="$2"
  local expected_digest="$3"
  local repo_digests
  repo_digests="$(docker image inspect "$image" \
    --format '{{range .RepoDigests}}{{println .}}{{end}}' 2>/dev/null || true)"
  grep -Fxq "${repository}@${expected_digest}" <<< "$repo_digests" || \
    die "image digest mismatch: $image"
}

set_env_value() {
  # 镜像值只允许来自固定前缀和完整 SHA，因此不会把 shell 语法带入 .env；使用
  # awk 重写临时文件，避免在 sed 参数中接触服务器密码等其他配置项。
  local env_file="$1"
  local key="$2"
  local value="$3"
  local replacement_file="${env_file}.tmp.$$"
  awk -v key="$key" -v value="$value" '
    BEGIN { replaced = 0 }
    {
      line = $0
      trimmed = line
      sub(/^[[:space:]]*/, "", trimmed)
      separator = index(trimmed, "=")
      candidate = ""
      if (separator > 1) {
        candidate = substr(trimmed, 1, separator - 1)
        sub(/[[:space:]]+$/, "", candidate)
      }
      if (candidate == key) {
        if (!replaced) print key "=" value
        replaced = 1
        next
      }
      print line
    }
    END {
      if (!replaced) print key "=" value
    }
  ' "$env_file" > "$replacement_file"
  chmod 600 "$replacement_file"
  mv -f -- "$replacement_file" "$env_file"
}

configure_alertmanager() {
  # 外部 Webhook 是可选能力：只有服务器上的 root 持有文件存在且包含非空文本时，
  # 才启用正式配置；否则使用仓库内的 noop 配置，避免监控服务因缺少秘密而无法启动。
  local env_file="$1"
  local webhook_file active_webhook_file
  webhook_file="$(env_value "$env_file" OPENREVIEWER_ALERT_WEBHOOK_URL_SOURCE_FILE 2>/dev/null || true)"
  if [[ -z "$webhook_file" ]]; then
    active_webhook_file="$(env_value "$env_file" OPENREVIEWER_ALERT_WEBHOOK_URL_FILE 2>/dev/null || true)"
    if [[ "$active_webhook_file" == /* ]]; then
      webhook_file="$active_webhook_file"
      set_env_value "$env_file" OPENREVIEWER_ALERT_WEBHOOK_URL_SOURCE_FILE \
        "$webhook_file"
    fi
  fi
  if [[ "$webhook_file" == /* && -f "$webhook_file" && ! -L "$webhook_file" ]] && \
    grep -q '[^[:space:]]' "$webhook_file"; then
    set_env_value "$env_file" OPENREVIEWER_ALERTMANAGER_CONFIG_FILE \
      "./observability/alertmanager.yml"
    set_env_value "$env_file" OPENREVIEWER_ALERT_WEBHOOK_URL_FILE \
      "$webhook_file"
    printf 'external alert notifications enabled\n'
  else
    set_env_value "$env_file" OPENREVIEWER_ALERTMANAGER_CONFIG_FILE \
      "./observability/alertmanager-noop.yml"
    set_env_value "$env_file" OPENREVIEWER_ALERT_WEBHOOK_URL_FILE \
      "./observability/alert-webhook-url.placeholder"
    printf 'external alert notifications disabled; no webhook secret file\n'
  fi
}

container_health() {
  local container_name="$1"
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' \
    "$container_name" 2>/dev/null || true
}

wait_healthy() {
  local container_name="$1"
  local timeout_seconds="${2:-180}"
  local started_at="$SECONDS"
  local state

  while (( SECONDS - started_at < timeout_seconds )); do
    state="$(container_health "$container_name")"
    case "$state" in
      healthy)
        return 0
        ;;
      unhealthy)
        printf 'container became unhealthy: %s\n' "$container_name" >&2
        docker logs --tail 80 "$container_name" >&2 || true
        return 1
        ;;
      no-healthcheck)
        printf 'container has no healthcheck: %s\n' "$container_name" >&2
        return 1
        ;;
    esac
    sleep 5
  done

  printf 'timed out waiting for healthy container: %s\n' "$container_name" >&2
  docker logs --tail 80 "$container_name" >&2 || true
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

wait_compose_service_stopped() {
  local release_dir="$1"
  local timeout_seconds="${2:-180}"
  local started_at="$SECONDS"
  local service container_id state all_stopped
  local -a release_compose container_ids
  release_compose=(
    docker compose
    --project-directory "$release_dir"
    --env-file "$release_dir/.env"
    --file "$release_dir/compose.yml"
  )

  while (( SECONDS - started_at < timeout_seconds )); do
    all_stopped=1
    for service in api worker web; do
      mapfile -t container_ids < <(
        "${release_compose[@]}" ps --all --quiet "$service"
      )
      for container_id in "${container_ids[@]}"; do
        [[ -n "$container_id" ]] || continue
        state="$(docker inspect --format '{{.State.Status}}' "$container_id" 2>/dev/null || true)"
        case "$state" in
          running|restarting|paused|"" )
            all_stopped=0
            ;;
        esac
      done
    done
    (( all_stopped == 1 )) && return 0
    sleep 2
  done
  printf 'timed out waiting for application containers to stop: %s\n' "$release_dir" >&2
  return 1
}

stop_application_services() {
  local release_dir="$1"
  local timeout_seconds="$2"
  local -a release_compose
  release_compose=(
    docker compose
    --project-directory "$release_dir"
    --env-file "$release_dir/.env"
    --file "$release_dir/compose.yml"
  )
  "${release_compose[@]}" stop --timeout "$timeout_seconds" api worker web
  wait_compose_service_stopped "$release_dir" "$timeout_seconds"
}

wait_internal_http() {
  local url="$1"
  local timeout_seconds="${2:-120}"
  local started_at="$SECONDS"
  while (( SECONDS - started_at < timeout_seconds )); do
    if docker exec openreviewer-api python -c \
      "from urllib.request import urlopen; response = urlopen('$url', timeout=3); assert response.status == 200" \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
  done
  printf 'timed out waiting for internal endpoint: %s\n' "$url" >&2
  return 1
}

assert_compose_service_image() {
  local service_name="$1"
  local expected_image="$2"
  local container_id actual_image
  local -a container_ids
  mapfile -t container_ids < <("${compose_cmd[@]}" ps --all --quiet "$service_name")
  (( ${#container_ids[@]} > 0 )) || die "service has no containers: $service_name"
  for container_id in "${container_ids[@]}"; do
    actual_image="$(docker inspect --format '{{.Config.Image}}' "$container_id")"
    [[ "$actual_image" == "$expected_image" ]] || \
      die "${service_name} container image mismatch"
  done
}

remove_stale_migration_container() {
  local status
  status="$(docker inspect --format '{{.State.Status}}' openreviewer-migrate 2>/dev/null || true)"
  [[ -z "$status" ]] && return 0
  case "$status" in
    created|dead|exited)
      docker rm openreviewer-migrate >/dev/null
      ;;
    *)
      die "migration container is still active: ${status}"
      ;;
  esac
}

temporary_dir=""
restore_database=""
temporary_backup_file=""
temporary_backup_checksum=""
incomplete_backup_file=""
incomplete_backup_checksum=""
temporary_link=""
cleanup_temporary_resources() {
  set +e
  if [[ -n "$restore_database" ]]; then
    docker exec openreviewer-postgres dropdb --username openreviewer --if-exists \
      --force "$restore_database" >/dev/null 2>&1
    restore_database=""
  fi
  if [[ -n "$temporary_dir" && -d "$temporary_dir" ]]; then
    rm -rf -- "$temporary_dir"
  fi
  if [[ -n "$temporary_backup_file" ]]; then
    rm -f -- "$temporary_backup_file"
  fi
  if [[ -n "$temporary_backup_checksum" ]]; then
    rm -f -- "$temporary_backup_checksum"
  fi
  if [[ -n "$incomplete_backup_file" ]]; then
    rm -f -- "$incomplete_backup_file"
  fi
  if [[ -n "$incomplete_backup_checksum" ]]; then
    rm -f -- "$incomplete_backup_checksum"
  fi
  if [[ -n "$temporary_link" && -L "$temporary_link" ]]; then
    rm -f -- "$temporary_link"
  fi
}
trap cleanup_temporary_resources EXIT

create_verified_database_backup() {
  local backup_timestamp backup_name temporary_backup temporary_checksum
  local checksum table_count revision
  backup_timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_name="${backup_timestamp}-${commit_sha}.dump"
  temporary_backup="$BACKUPS_DIR/.${backup_name}.tmp.$$"
  temporary_checksum="$BACKUPS_DIR/.${backup_name}.sha256.tmp.$$"
  temporary_backup_file="$temporary_backup"
  temporary_backup_checksum="$temporary_checksum"
  backup_file="$BACKUPS_DIR/$backup_name"
  restore_database="openreviewer_verify_${commit_sha:0:12}_$$"

  [[ ! -e "$backup_file" && ! -e "${backup_file}.sha256" ]] || \
    die "database backup target already exists"
  incomplete_backup_file="$backup_file"
  incomplete_backup_checksum="${backup_file}.sha256"
  timeout --signal=TERM 1800 docker exec openreviewer-postgres \
    pg_dump --username openreviewer --dbname openreviewer --format=custom \
      --compress=6 --no-owner --no-privileges > "$temporary_backup"
  [[ -s "$temporary_backup" ]] || die "database backup is empty"
  chmod 600 "$temporary_backup"
  timeout --signal=TERM 120 docker exec -i openreviewer-postgres \
    pg_restore --list < "$temporary_backup" >/dev/null

  docker exec openreviewer-postgres createdb --username openreviewer \
    --template template0 "$restore_database"
  timeout --signal=TERM 1800 docker exec -i openreviewer-postgres \
    pg_restore --username openreviewer --dbname "$restore_database" \
      --exit-on-error --no-owner --no-privileges < "$temporary_backup"
  table_count="$(docker exec openreviewer-postgres psql --username openreviewer \
    --dbname "$restore_database" --no-align --tuples-only --set=ON_ERROR_STOP=1 \
    --command "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname = 'public';" \
    | tr -d '[:space:]')"
  [[ "$table_count" =~ ^[1-9][0-9]*$ ]] || die "restored backup contains no public tables"
  revision="$(docker exec openreviewer-postgres psql --username openreviewer \
    --dbname "$restore_database" --no-align --tuples-only --set=ON_ERROR_STOP=1 \
    --command 'SELECT version_num FROM alembic_version;' | tr -d '[:space:]')"
  [[ "$revision" =~ ^[0-9]{8}_[0-9]{4}$ ]] || die "restored backup has no valid migration revision"
  docker exec openreviewer-postgres dropdb --username openreviewer --if-exists \
    --force "$restore_database"
  restore_database=""

  checksum="$(sha256sum "$temporary_backup" | awk '{print $1}')"
  [[ "$checksum" =~ ^[0-9a-f]{64}$ ]] || die "database backup checksum failed"
  printf '%s  %s\n' "$checksum" "$backup_name" > "$temporary_checksum"
  chmod 600 "$temporary_checksum"
  mv -- "$temporary_backup" "$backup_file"
  mv -- "$temporary_checksum" "${backup_file}.sha256"
  temporary_backup_file=""
  temporary_backup_checksum=""
  incomplete_backup_file=""
  incomplete_backup_checksum=""
  printf 'verified database backup: %s revision=%s\n' "$backup_name" "$revision"
}

prune_database_backups() {
  local retention_count="$1"
  local candidate candidate_name
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
}

prune_releases() {
  # current 始终保留，再保留最近的若干个非当前 SHA 目录。只接受固定的
  # 40 位十六进制目录名，并拒绝符号链接，避免清理逻辑越出 releases 根目录。
  local retention_count="$1"
  local current_real current_name candidate candidate_name kept=0 keep_previous
  current_real="$(readlink -f -- "$CURRENT_LINK" 2>/dev/null || true)"
  current_name="${current_real##*/}"
  if [[ -z "$current_real" || "$current_real" != "$RELEASES_DIR/$current_name" || \
    ! "$current_name" =~ ^[0-9a-f]{40}$ ]]; then
    printf 'release cleanup skipped: current link is outside managed releases\n' >&2
    return 1
  fi
  keep_previous=$((retention_count - 1))
  while IFS= read -r candidate; do
    candidate_name="${candidate#"$RELEASES_DIR/"}"
    if [[ "$candidate" != "$RELEASES_DIR/$candidate_name" || \
      ! "$candidate_name" =~ ^[0-9a-f]{40}$ || ! -d "$candidate" || -L "$candidate" ]]; then
      printf 'release cleanup skipped: unexpected release path\n' >&2
      return 1
    fi
    [[ "$candidate" == "$current_real" ]] && continue
    if (( kept < keep_previous )); then
      kept=$((kept + 1))
      continue
    fi
    if ! rm -rf -- "$candidate"; then
      printf 'release cleanup skipped: cannot remove %s\n' "$candidate_name" >&2
      return 1
    fi
  done < <(
    find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d \
      -name '????????????????????????????????????????' \
      -printf '%T@ %p\n' \
      | LC_ALL=C sort -rn \
      | awk '{ sub(/^[^ ]+ /, ""); print }'
  )
}

mkdir -p -- "$RELEASES_DIR" "$BACKUPS_DIR"
chmod 700 "$BACKUPS_DIR"
temporary_dir="$(mktemp -d "$RELEASES_DIR/.deploy-${commit_sha}.XXXXXX")"
temporary_compose="$temporary_dir/compose.yml"
receive_release_bundle "$temporary_dir"

# 只接受本项目的 Compose 文件，避免受限 SSH key 被用来提交任意 Compose 配置。
grep -q '^name: openreviewer$' "$temporary_compose" || die "unexpected compose project name"
grep -q 'OPENREVIEWER_IMAGE' "$temporary_compose" || die "API image variable is missing"
grep -q 'OPENREVIEWER_WEB_IMAGE' "$temporary_compose" || die "Web image variable is missing"

# 先完整接收并做最小校验，再持有发布锁；网络输入中断不能阻塞后续正常发布。
exec 9>"$LOCK_FILE"
flock -w 300 9 || die "another deployment is still running"
verify_installed_helpers "$temporary_dir/helper-digests.env"

previous_release="$(readlink -f -- "$CURRENT_LINK" 2>/dev/null || true)"
[[ -f "$previous_release/.env" ]] || die "current release does not contain .env"
[[ -f "$previous_release/compose.yml" ]] || die "current release does not contain compose.yml"
validate_env_file "$previous_release/.env" || \
  die "current release .env contains invalid or duplicate keys"

release_dir="$RELEASES_DIR/$commit_sha"
if [[ -e "$release_dir" ]]; then
  # 同一 SHA 的重试必须使用完全相同的部署文件，禁止覆盖不可变发布目录。
  [[ -f "$release_dir/.env" && -f "$release_dir/compose.yml" ]] || die "incomplete existing release"
  for release_file in "${RELEASE_BUNDLE_FILES[@]}"; do
    [[ -f "$release_dir/$release_file" && ! -L "$release_dir/$release_file" ]] || \
      die "existing release is missing deployment file: $release_file"
    cmp -s "$temporary_dir/$release_file" "$release_dir/$release_file" || \
      die "existing release differs: $release_file"
  done
  assert_env_value "$release_dir/.env" OPENREVIEWER_IMAGE \
    "${EXPECTED_API_IMAGE_PREFIX}${commit_sha}"
  assert_env_value "$release_dir/.env" OPENREVIEWER_WEB_IMAGE \
    "${EXPECTED_WEB_IMAGE_PREFIX}${commit_sha}"
  rm -rf -- "$temporary_dir"
  temporary_dir=""
else
  cp --preserve=mode "$previous_release/.env" "$temporary_dir/.env"
  chmod 600 "$temporary_dir/.env"
  set_env_value "$temporary_dir/.env" OPENREVIEWER_IMAGE "${EXPECTED_API_IMAGE_PREFIX}${commit_sha}"
  set_env_value "$temporary_dir/.env" OPENREVIEWER_WEB_IMAGE "${EXPECTED_WEB_IMAGE_PREFIX}${commit_sha}"
  configure_alertmanager "$temporary_dir/.env"
  mv -- "$temporary_dir" "$release_dir"
  temporary_dir=""
fi

api_image="${EXPECTED_API_IMAGE_PREFIX}${commit_sha}"
web_image="${EXPECTED_WEB_IMAGE_PREFIX}${commit_sha}"
assert_env_value "$release_dir/.env" OPENREVIEWER_IMAGE "$api_image"
assert_env_value "$release_dir/.env" OPENREVIEWER_WEB_IMAGE "$web_image"
api_repository="${EXPECTED_API_IMAGE_PREFIX%:}"
web_repository="${EXPECTED_WEB_IMAGE_PREFIX%:}"
api_digest="$(read_image_digest "$release_dir/image-digests.env" OPENREVIEWER_API_IMAGE_DIGEST)" || \
  die "image digest manifest is invalid"
web_digest="$(read_image_digest "$release_dir/image-digests.env" OPENREVIEWER_WEB_IMAGE_DIGEST)" || \
  die "image digest manifest is invalid"
deploy_helper_digest="$(read_helper_digest "$release_dir/helper-digests.env" \
  OPENREVIEWER_DEPLOY_HELPER_SHA256)" || die "helper digest manifest is invalid"
backup_helper_digest="$(read_helper_digest "$release_dir/helper-digests.env" \
  OPENREVIEWER_BACKUP_HELPER_SHA256)" || die "helper digest manifest is invalid"
restore_helper_digest="$(read_helper_digest "$release_dir/helper-digests.env" \
  OPENREVIEWER_RESTORE_HELPER_SHA256)" || die "helper digest manifest is invalid"
compose_cmd=(docker compose --project-directory "$release_dir" --env-file "$release_dir/.env" --file "$release_dir/compose.yml")
worker_replicas="$(read_worker_replicas "$release_dir/.env")" || die "invalid worker replica count"
deploy_stop_timeout="$(read_deploy_stop_timeout "$release_dir/.env")" || \
  die "invalid deployment stop timeout"
if [[ "$previous_release" == "$release_dir" ]]; then
  # Actions 重试可能再次投递已经完成的 SHA；重新核对应用镜像并校正全部服务，
  # 这样 API、Web 或监控服务单独被宿主机重启后也能自愈。
  worker_replicas="$(read_worker_replicas "$release_dir/.env")" || die "invalid worker replica count"
  "${compose_cmd[@]}" config --quiet
  docker pull "${api_repository}@${api_digest}"
  docker tag "${api_repository}@${api_digest}" "$api_image"
  docker pull "${web_repository}@${web_digest}"
  docker tag "${web_repository}@${web_digest}" "$web_image"
  assert_image_digest "$api_image" "$api_repository" "$api_digest"
  assert_image_digest "$web_image" "$web_repository" "$web_digest"
  "${compose_cmd[@]}" up -d --no-deps --scale "worker=$worker_replicas" \
    api worker web prometheus alertmanager grafana
  wait_healthy openreviewer-api 60
  wait_compose_service_healthy worker 60
  wait_healthy openreviewer-web 60
  wait_internal_http http://prometheus:9090/-/ready 60
  wait_internal_http http://alertmanager:9093/-/ready 60
  wait_internal_http http://grafana:3000/api/health 60
  printf 'release already active: %s\n' "$commit_sha"
  exit 0
fi

"${compose_cmd[@]}" config --quiet
services="$("${compose_cmd[@]}" config --services | LC_ALL=C sort | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
[[ "$services" == "alertmanager api grafana migrate postgres prometheus web worker" ]] || \
  die "unexpected compose services"
"${compose_cmd[@]}" pull postgres migrate prometheus alertmanager grafana
docker pull "${api_repository}@${api_digest}"
docker tag "${api_repository}@${api_digest}" "$api_image"
docker pull "${web_repository}@${web_digest}"
docker tag "${web_repository}@${web_digest}" "$web_image"
assert_image_digest "$api_image" "$api_repository" "$api_digest"
assert_image_digest "$web_image" "$web_repository" "$web_digest"

rollout_started=0
migration_started=0
migration_completed=0
on_error() {
  local status="$1"
  trap - ERR
  if (( rollout_started == 1 )); then
    local -a failed_compose=("${compose_cmd[@]}")
    if (( migration_started == 1 && migration_completed == 0 )); then
      # 迁移可能已部分提交 DDL；此时不能假设旧版本仍兼容，宁可保持应用停止，
      # 交给管理员核对数据库版本和迁移日志后再决定启动哪个版本。
      printf 'database migration did not complete; application services remain stopped for manual recovery\n' >&2
      set +e
      "${failed_compose[@]}" stop api worker web prometheus alertmanager grafana \
        >/dev/null 2>&1
      set -e
      exit "$status"
    fi
  fi
  if (( rollout_started == 1 )) && [[ -n "$previous_release" && -f "$previous_release/.env" ]]; then
    printf 'new release failed; restoring previous application containers\n' >&2
    local -a previous_compose=(docker compose --project-directory "$previous_release" --env-file "$previous_release/.env" --file "$previous_release/compose.yml")
    local -a rollback_services=(api worker web)
    local previous_services optional_service
    set +e
    previous_services="$("${previous_compose[@]}" config --services 2>/dev/null)"
    for optional_service in prometheus alertmanager grafana; do
      if grep -Fxq "$optional_service" <<< "$previous_services"; then
        rollback_services+=("$optional_service")
      else
        "${failed_compose[@]}" stop "$optional_service" >/dev/null 2>&1
      fi
    done
    previous_worker_replicas="$(read_worker_replicas "$previous_release/.env" 2>/dev/null || printf '1')"
    "${previous_compose[@]}" up -d --no-deps \
      --scale "worker=$previous_worker_replicas" "${rollback_services[@]}" >/dev/null 2>&1
    wait_healthy openreviewer-api 120 >/dev/null 2>&1
    compose_cmd=("${previous_compose[@]}")
    wait_compose_service_healthy worker 120 >/dev/null 2>&1
    wait_healthy openreviewer-web 120 >/dev/null 2>&1
    if grep -Fxq prometheus <<< "$previous_services"; then
      wait_internal_http http://prometheus:9090/-/ready 60 >/dev/null 2>&1
    fi
    if grep -Fxq alertmanager <<< "$previous_services"; then
      wait_internal_http http://alertmanager:9093/-/ready 60 >/dev/null 2>&1
    fi
    if grep -Fxq grafana <<< "$previous_services"; then
      wait_internal_http http://grafana:3000/api/health 60 >/dev/null 2>&1
    fi
    set -e
  fi
  exit "$status"
}
trap 'on_error "$?"' ERR

# 先在原数据库镜像上验证备份，再停止应用并切换数据库镜像。
# 不让 Compose 因 depends_on 自动重复执行 migrate。
wait_healthy openreviewer-postgres 180
backup_file=""
create_verified_database_backup
rollout_started=1
# 迁移前必须先让旧版本应用完全退出。否则旧 Worker 可能在新表结构已经部分
# 变更时继续领取任务或发起模型请求，形成不可审计的双版本并行窗口。
stop_application_services "$previous_release" "$deploy_stop_timeout"
"${compose_cmd[@]}" up -d postgres
wait_healthy openreviewer-postgres 180
remove_stale_migration_container
migration_started=1
"${compose_cmd[@]}" run --rm --no-deps migrate
migration_completed=1

"${compose_cmd[@]}" up -d --no-deps --scale "worker=$worker_replicas" \
  api worker web prometheus alertmanager grafana
wait_healthy openreviewer-api 180
wait_compose_service_healthy worker 180
wait_healthy openreviewer-web 180
wait_internal_http http://prometheus:9090/-/ready 180
wait_internal_http http://alertmanager:9093/-/ready 180
wait_internal_http http://grafana:3000/api/health 180

api_host_port="$(env_value "$release_dir/.env" OPENREVIEWER_API_HOST_PORT 2>/dev/null || printf '18090')"
web_host_port="$(env_value "$release_dir/.env" OPENREVIEWER_WEB_HOST_PORT 2>/dev/null || printf '18443')"
[[ "$api_host_port" =~ ^[0-9]+$ ]] || die "invalid API host port"
[[ "$web_host_port" =~ ^[0-9]+$ ]] || die "invalid Web host port"
curl --fail --silent --show-error --max-time 15 "http://127.0.0.1:${api_host_port}/readyz" >/dev/null
curl --fail --silent --show-error --insecure --max-time 15 \
  "https://127.0.0.1:${web_host_port}/healthz" >/dev/null

[[ "$(docker inspect --format '{{.Config.Image}}' openreviewer-api)" == "$api_image" ]] || die "API container image mismatch"
assert_compose_service_image worker "$api_image"
[[ "$(docker inspect --format '{{.Config.Image}}' openreviewer-web)" == "$web_image" ]] || die "Web container image mismatch"

migration_version="$(docker exec openreviewer-api python -m alembic current | tail -n 1 | tr -d '\r')"
{
  printf 'commit=%s\n' "$commit_sha"
  printf 'api_image=%s\n' "${api_repository}@${api_digest}"
  printf 'web_image=%s\n' "${web_repository}@${web_digest}"
  printf 'deploy_helper_sha256=%s\n' "$deploy_helper_digest"
  printf 'backup_helper_sha256=%s\n' "$backup_helper_digest"
  printf 'restore_helper_sha256=%s\n' "$restore_helper_digest"
  printf 'migration=%s\n' "$migration_version"
  printf 'database_backup=%s\n' "$(basename "$backup_file")"
  printf 'deployed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$release_dir/release.info"
chmod 644 "$release_dir/release.info"

backup_retention_count="$(env_value "$release_dir/.env" OPENREVIEWER_BACKUP_RETENTION_COUNT 2>/dev/null || printf '14')"
[[ "$backup_retention_count" =~ ^[0-9]+$ ]] || die "invalid database backup retention count"
(( backup_retention_count >= 3 && backup_retention_count <= 100 )) || \
  die "database backup retention count must be between 3 and 100"
prune_database_backups "$backup_retention_count"

release_retention_count="$(env_value "$release_dir/.env" OPENREVIEWER_RELEASE_RETENTION_COUNT 2>/dev/null || printf '5')"
[[ "$release_retention_count" =~ ^[0-9]+$ ]] || die "invalid release retention count"
(( release_retention_count >= 2 && release_retention_count <= 100 )) || \
  die "release retention count must be between 2 and 100"

# 只有所有检查都通过才替换 current；mv 在同一文件系统内是原子的，读取者不会看到半个链接。
temporary_link="$BASE_DIR/.current-${commit_sha}.$$"
ln -s "releases/$commit_sha" "$temporary_link"
mv -Tf -- "$temporary_link" "$CURRENT_LINK"
temporary_link=""
rollout_started=0
trap - ERR
if ! prune_releases "$release_retention_count"; then
  printf 'warning: release cleanup did not complete; current deployment remains active\n' >&2
fi
printf 'deployed commit=%s migration=%s\n' "$commit_sha" "$migration_version"
