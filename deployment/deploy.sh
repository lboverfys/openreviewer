#!/usr/bin/env bash
###############################################################################
# OpenReviewer niuma-2 发布入口
#
# GitHub Actions 通过受限 SSH 密钥执行本脚本。脚本从标准输入接收本次提交
# 的 deployment/compose.yml，从当前发布复制服务器专有的 .env，只替换两个
# 镜像标签，然后执行迁移、滚动启动和健康检查。
#
# 设计约束：
# 1. 每个提交使用独立的不可变 releases/<sha> 目录；current 只在新版本健康后
#    原子切换，因此失败版本不会覆盖上一个可用版本。
# 2. 数据库迁移只向前执行。回滚应用容器时不执行 alembic downgrade，避免
#    自动破坏已经升级的数据结构。
# 3. 脚本不读取或打印 .env 的具体内容，也不把密码、会话密钥写入 release.info。
###############################################################################

set -Eeuo pipefail
umask 077

BASE_DIR="${OPENREVIEWER_BASE_DIR:-/opt/openreviewer}"
RELEASES_DIR="$BASE_DIR/releases"
CURRENT_LINK="$BASE_DIR/current"
LOCK_FILE="$BASE_DIR/deploy.lock"
EXPECTED_API_IMAGE_PREFIX="ghcr.io/lboverfys/openreviewer:"
EXPECTED_WEB_IMAGE_PREFIX="ghcr.io/lboverfys/openreviewer-web:"

die() {
  printf 'OpenReviewer deployment failed: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

for command_name in awk cat chmod cmp cp curl date docker flock grep id ln mkdir mktemp mv \
  readlink rm sed sleep sort tail timeout tr wc; do
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

require_file_from_stdin() {
  local target_file="$1"
  [[ -t 0 ]] && die "compose.yml must be supplied through SSH stdin"
  if ! timeout --signal=TERM 30 cat > "$target_file"; then
    die "timed out while receiving compose.yml"
  fi
  [[ -s "$target_file" ]] || die "received compose.yml is empty"
  [[ "$(wc -c < "$target_file")" -le 262144 ]] || die "received compose.yml is too large"
}

env_value() {
  # 只读取非敏感的发布字段；不会把服务器密码或会话密钥打印到日志。
  local env_file="$1"
  local key="$2"
  awk -F= -v key="$key" '
    $1 == key {
      value = substr($0, index($0, "=") + 1)
      print value
      found = 1
      exit
    }
    END { if (!found) exit 1 }
  ' "$env_file"
}

assert_env_value() {
  local env_file="$1"
  local key="$2"
  local expected="$3"
  local actual
  actual="$(env_value "$env_file" "$key" 2>/dev/null || true)"
  [[ "$actual" == "$expected" ]] || die "${key} does not match this commit"
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
    index($0, key "=") == 1 {
      print key "=" value
      replaced = 1
      next
    }
    { print }
    END {
      if (!replaced) print key "=" value
    }
  ' "$env_file" > "$replacement_file"
  chmod 600 "$replacement_file"
  mv -f -- "$replacement_file" "$env_file"
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
cleanup_temporary_dir() {
  if [[ -n "$temporary_dir" && -d "$temporary_dir" ]]; then
    rm -rf -- "$temporary_dir"
  fi
}
trap cleanup_temporary_dir EXIT

mkdir -p -- "$RELEASES_DIR"
temporary_dir="$(mktemp -d "$RELEASES_DIR/.deploy-${commit_sha}.XXXXXX")"
temporary_compose="$temporary_dir/compose.yml"
require_file_from_stdin "$temporary_compose"
chmod 644 "$temporary_compose"

# 只接受本项目的 Compose 文件，避免受限 SSH key 被用来提交任意 Compose 配置。
grep -q '^name: openreviewer$' "$temporary_compose" || die "unexpected compose project name"
grep -q 'OPENREVIEWER_IMAGE' "$temporary_compose" || die "API image variable is missing"
grep -q 'OPENREVIEWER_WEB_IMAGE' "$temporary_compose" || die "Web image variable is missing"

# 先完整接收并做最小校验，再持有发布锁；网络输入中断不能阻塞后续正常发布。
exec 9>"$LOCK_FILE"
flock -w 300 9 || die "another deployment is still running"

previous_release="$(readlink -f -- "$CURRENT_LINK" 2>/dev/null || true)"
[[ -f "$previous_release/.env" ]] || die "current release does not contain .env"
[[ -f "$previous_release/compose.yml" ]] || die "current release does not contain compose.yml"

release_dir="$RELEASES_DIR/$commit_sha"
if [[ -e "$release_dir" ]]; then
  # 同一 SHA 的重试必须使用完全相同的 Compose 文件，禁止覆盖不可变发布目录。
  [[ -f "$release_dir/.env" && -f "$release_dir/compose.yml" ]] || die "incomplete existing release"
  cmp -s "$temporary_compose" "$release_dir/compose.yml" || die "existing release differs"
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
  mv -- "$temporary_dir" "$release_dir"
  temporary_dir=""
fi

api_image="${EXPECTED_API_IMAGE_PREFIX}${commit_sha}"
web_image="${EXPECTED_WEB_IMAGE_PREFIX}${commit_sha}"
assert_env_value "$release_dir/.env" OPENREVIEWER_IMAGE "$api_image"
assert_env_value "$release_dir/.env" OPENREVIEWER_WEB_IMAGE "$web_image"
if [[ "$previous_release" == "$release_dir" ]]; then
  # Actions 重试可能再次投递已经完成的 SHA；健康检查后直接幂等成功。
  wait_healthy openreviewer-api 60
  wait_healthy openreviewer-worker 60
  wait_healthy openreviewer-web 60
  printf 'release already active: %s\n' "$commit_sha"
  exit 0
fi

compose_cmd=(docker compose --project-directory "$release_dir" --env-file "$release_dir/.env" --file "$release_dir/compose.yml")
"${compose_cmd[@]}" config --quiet
services="$("${compose_cmd[@]}" config --services | LC_ALL=C sort | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
[[ "$services" == "api migrate postgres web worker" ]] || die "unexpected compose services"
"${compose_cmd[@]}" pull postgres migrate api worker web

rollout_started=0
on_error() {
  local status="$1"
  trap - ERR
  if (( rollout_started == 1 )) && [[ -n "$previous_release" && -f "$previous_release/.env" ]]; then
    printf 'new release failed; restoring previous application containers\n' >&2
    local previous_compose=(docker compose --project-directory "$previous_release" --env-file "$previous_release/.env" --file "$previous_release/compose.yml")
    set +e
    "${previous_compose[@]}" up -d --no-deps api worker web >/dev/null 2>&1
    wait_healthy openreviewer-api 120 >/dev/null 2>&1
    wait_healthy openreviewer-worker 120 >/dev/null 2>&1
    wait_healthy openreviewer-web 120 >/dev/null 2>&1
    set -e
  fi
  exit "$status"
}
trap 'on_error "$?"' ERR

# 先确保 PostgreSQL 健康，再执行一次迁移；不让 Compose 因 depends_on 自动重复执行
# migrate，便于把迁移失败明确归因到本次发布。
"${compose_cmd[@]}" up -d postgres
wait_healthy openreviewer-postgres 180
remove_stale_migration_container
"${compose_cmd[@]}" run --rm --no-deps migrate

rollout_started=1
"${compose_cmd[@]}" up -d --no-deps api worker web
wait_healthy openreviewer-api 180
wait_healthy openreviewer-worker 180
wait_healthy openreviewer-web 180

api_host_port="$(env_value "$release_dir/.env" OPENREVIEWER_API_HOST_PORT 2>/dev/null || printf '18090')"
web_host_port="$(env_value "$release_dir/.env" OPENREVIEWER_WEB_HOST_PORT 2>/dev/null || printf '18443')"
[[ "$api_host_port" =~ ^[0-9]+$ ]] || die "invalid API host port"
[[ "$web_host_port" =~ ^[0-9]+$ ]] || die "invalid Web host port"
curl --fail --silent --show-error --max-time 15 "http://127.0.0.1:${api_host_port}/healthz" >/dev/null
curl --fail --silent --show-error --insecure --max-time 15 \
  "https://127.0.0.1:${web_host_port}/healthz" >/dev/null

[[ "$(docker inspect --format '{{.Config.Image}}' openreviewer-api)" == "$api_image" ]] || die "API container image mismatch"
[[ "$(docker inspect --format '{{.Config.Image}}' openreviewer-worker)" == "$api_image" ]] || die "Worker container image mismatch"
[[ "$(docker inspect --format '{{.Config.Image}}' openreviewer-web)" == "$web_image" ]] || die "Web container image mismatch"

migration_version="$(docker exec openreviewer-api python -m alembic current | tail -n 1 | tr -d '\r')"
api_digest="$(docker image inspect "$api_image" --format '{{index .RepoDigests 0}}')"
web_digest="$(docker image inspect "$web_image" --format '{{index .RepoDigests 0}}')"
{
  printf 'commit=%s\n' "$commit_sha"
  printf 'api_image=%s\n' "$api_digest"
  printf 'web_image=%s\n' "$web_digest"
  printf 'migration=%s\n' "$migration_version"
  printf 'deployed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$release_dir/release.info"
chmod 644 "$release_dir/release.info"

# 只有所有检查都通过才替换 current；mv 在同一文件系统内是原子的，读取者不会看到半个链接。
temporary_link="$BASE_DIR/.current-${commit_sha}.$$"
ln -s "releases/$commit_sha" "$temporary_link"
mv -Tf -- "$temporary_link" "$CURRENT_LINK"
rollout_started=0
trap - ERR
printf 'deployed commit=%s migration=%s\n' "$commit_sha" "$migration_version"
