from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from persistence.models import ReviewQuotaBucketRecord

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT_ROOT = PROJECT_ROOT / "deployment"


def deployment_text(relative_path: str) -> str:
    return (DEPLOYMENT_ROOT / relative_path).read_text(encoding="utf-8")


def project_text(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_review_quota_postgresql_ddl_quotes_window_keyword() -> None:
    ddl = str(
        CreateTable(ReviewQuotaBucketRecord.__table__).compile(
            dialect=postgresql.dialect()
        )
    )

    assert 'CHECK ("window" IN (\'hour\', \'day\'))' in ddl


def test_alertmanager_uses_a_read_only_webhook_secret() -> None:
    configuration = deployment_text("observability/alertmanager.yml")
    noop_configuration = deployment_text("observability/alertmanager-noop.yml")
    placeholder = deployment_text("observability/alert-webhook-url.placeholder")
    compose = deployment_text("compose.yml")
    deploy = deployment_text("deploy.sh")

    assert "url_file: /run/secrets/alert-webhook-url" in configuration
    assert "send_resolved: true" in configuration
    assert "OPENREVIEWER_ALERTMANAGER_CONFIG_FILE:-./observability/alertmanager-noop.yml" in compose
    assert "OPENREVIEWER_ALERT_WEBHOOK_URL_FILE:-./observability/alert-webhook-url.placeholder" in compose
    assert "target: /run/secrets/alert-webhook-url" in compose
    assert "alert-webhook-url\n        read_only: true" in compose
    assert 'user: "65534:0"' in compose
    assert "receiver: openreviewer-noop" in noop_configuration
    assert "webhook_configs" not in noop_configuration
    assert placeholder.strip()
    assert "configure_alertmanager" in deploy
    assert "OPENREVIEWER_ALERT_WEBHOOK_URL_SOURCE_FILE" in deploy
    assert "external alert notifications disabled" in deploy


def test_periodic_backup_verifies_restore_before_atomic_publish() -> None:
    backup = deployment_text("backup.sh")

    assert "pg_dump --username openreviewer" in backup
    assert "pg_restore --list" in backup
    assert "--exit-on-error --no-owner --no-privileges" in backup
    assert "SELECT version_num FROM alembic_version" in backup
    assert backup.index('mv -- "$temporary_backup" "$backup_file"') > backup.index(
        "SELECT version_num FROM alembic_version"
    )


def test_backup_mirror_is_an_executable_path_not_evaluated_shell() -> None:
    backup = deployment_text("backup.sh")

    assert "^/usr/local/libexec/[A-Za-z0-9._/-]+$" in backup
    assert 'resolved_command="$(readlink -f -- "$mirror_command"' in backup
    assert '"$resolved_command" == "$mirror_command"' in backup
    assert 'timeout --signal=TERM 1800 "$mirror_command"' in backup
    assert "eval " not in backup
    assert "OPENREVIEWER_BACKUP_REQUIRE_MIRROR" in backup


def test_backup_timer_is_persistent_and_bounded() -> None:
    service = deployment_text("openreviewer-backup.service")
    timer = deployment_text("openreviewer-backup.timer")

    assert "TimeoutStartSec=45min" in service
    assert "NoNewPrivileges=true" in service
    assert "OnCalendar=*-*-* 03:15:00 UTC" in timer
    assert "RandomizedDelaySec=15m" in timer
    assert "Persistent=true" in timer


def test_restore_waits_for_every_worker_replica() -> None:
    restore = deployment_text("restore.sh")

    assert 'wait_compose_service_healthy worker 180' in restore
    assert 'wait_healthy openreviewer-worker 180' not in restore
    assert 'ps --all --quiet "$service_name"' in restore
    assert '--scale "worker=$worker_replicas"' in restore


def test_deploy_scales_workers_and_validates_the_replica_bound() -> None:
    deploy = deployment_text("deploy.sh")

    assert "read_worker_replicas" in deploy
    assert "replicas >= 1 && replicas <= 20" in deploy
    assert '--scale "worker=$worker_replicas"' in deploy
    assert '--scale "worker=$previous_worker_replicas"' in deploy


def test_deploy_bounds_release_retention_and_never_removes_current() -> None:
    deploy = deployment_text("deploy.sh")

    assert "prune_releases" in deploy
    assert "OPENREVIEWER_RELEASE_RETENTION_COUNT" in deploy
    assert "release_retention_count >= 2 && release_retention_count <= 100" in deploy
    assert '[[ "$candidate" == "$current_real" ]] && continue' in deploy
    assert "release cleanup skipped: current link is outside managed releases" in deploy


def test_restore_bounds_pre_restore_backup_growth() -> None:
    restore = deployment_text("restore.sh")

    assert "prune_emergency_backups" in restore
    assert "pre-restore" in restore
    assert 'prune_emergency_backups "$retention_count" "$backup_file"' in restore
    assert '[[ "$candidate" == "$protected_file" ]] && continue' in restore
    assert "emergency backup cleanup skipped" in restore


def test_deploy_drains_old_application_before_running_migrations() -> None:
    deploy = deployment_text("deploy.sh")
    compose = deployment_text("compose.yml")
    environment = deployment_text(".env.example")

    assert "read_deploy_stop_timeout" in deploy
    assert "stop_application_services" in deploy
    assert 'stop --timeout "$timeout_seconds" api worker web' in deploy
    stop_position = deploy.index('stop_application_services "$previous_release"')
    migration_position = deploy.index('run --rm --no-deps migrate')
    assert stop_position < migration_position
    assert "wait_compose_service_stopped" in deploy
    assert "stop_grace_period: 16m" in compose
    assert "OPENREVIEWER_DEPLOY_STOP_TIMEOUT_SECONDS=960" in environment


def test_same_sha_retry_reconciles_every_runtime_service() -> None:
    deploy = deployment_text("deploy.sh")

    retry_position = deploy.index('if [[ "$previous_release" == "$release_dir" ]]')
    retry_end = deploy.index("  printf 'release already active", retry_position)
    retry_block = deploy[retry_position:retry_end]
    assert 'up -d --no-deps --scale "worker=$worker_replicas"' in retry_block
    for service in ("api", "worker", "web", "prometheus", "alertmanager", "grafana"):
        assert service in retry_block
    assert "docker pull \"${api_repository}@${api_digest}\"" in retry_block
    assert "docker pull \"${web_repository}@${web_digest}\"" in retry_block


def test_deployment_rejects_ambiguous_environment_files() -> None:
    deploy = deployment_text("deploy.sh")
    backup = deployment_text("backup.sh")
    restore = deployment_text("restore.sh")

    for script in (deploy, backup, restore):
        assert "validate_env_file" in script
        assert "duplicate" in script
        assert "invalid or duplicate keys" in script
    assert "if (++seen[key] > 1) duplicate = 1" in deploy
    assert "if (!replaced) print key \"=\" value" in deploy


def test_failed_migration_keeps_services_stopped_for_manual_recovery() -> None:
    deploy = deployment_text("deploy.sh")

    assert "migration_started=0" in deploy
    assert "migration_completed=0" in deploy
    assert "migration_started=1" in deploy
    assert "migration_completed=1" in deploy
    assert "database migration did not complete; application services remain stopped" in deploy
    assert 'stop api worker web prometheus alertmanager grafana' in deploy


def test_restore_drains_web_before_switching_databases() -> None:
    restore = deployment_text("restore.sh")

    assert 'stop api worker web' in restore


def test_restore_checksum_is_bound_to_the_requested_backup() -> None:
    restore = deployment_text("restore.sh")

    assert 'expected_name="$backup_name"' in restore
    assert 'line_count != 1 || match_count != 1' in restore
    assert 'sha256sum "$backup_file"' in restore


def test_release_bundle_rejects_non_regular_members_before_extracting() -> None:
    deploy = deployment_text("deploy.sh")
    workflow = project_text(".github/workflows/verify-and-publish.yml")

    type_check = 'tar --list --verbose --file "$archive_file"'
    extraction = 'tar --extract --file "$archive_file"'
    assert type_check in deploy
    assert deploy.index(type_check) < deploy.index(extraction)
    for member in (
        "image-digests.env",
        "helper-digests.env",
        "observability/alertmanager-noop.yml",
        "observability/alert-webhook-url.placeholder",
    ):
        assert member in deploy
        assert member in workflow


def test_deployment_verifies_ci_image_digests() -> None:
    deploy = deployment_text("deploy.sh")
    workflow = project_text(".github/workflows/verify-and-publish.yml")

    assert "read_image_digest" in deploy
    assert "assert_image_digest" in deploy
    assert 'docker pull "${api_repository}@${api_digest}"' in deploy
    assert 'docker pull "${web_repository}@${web_digest}"' in deploy
    assert "OPENREVIEWER_API_IMAGE_DIGEST" in workflow
    assert "OPENREVIEWER_WEB_IMAGE_DIGEST" in workflow
    assert "steps.api-image.outputs.digest" in workflow
    assert "steps.web-image.outputs.digest" in workflow
    tar_start = workflow.index("tar --create --file -")
    manifest_position = workflow.index(
        '--directory "$manifest_dir" image-digests.env',
        tar_start,
    )
    deployment_position = workflow.index(
        '--directory "$GITHUB_WORKSPACE/deployment"',
        manifest_position,
    )
    assert manifest_position < deployment_position


def test_deployment_rejects_operator_script_version_drift() -> None:
    deploy = deployment_text("deploy.sh")
    workflow = project_text(".github/workflows/verify-and-publish.yml")

    assert "helper-digests.env" in deploy
    assert "verify_installed_helpers" in deploy
    assert "installed helper version drift" in deploy
    for key in (
        "OPENREVIEWER_DEPLOY_HELPER_SHA256",
        "OPENREVIEWER_BACKUP_HELPER_SHA256",
        "OPENREVIEWER_RESTORE_HELPER_SHA256",
    ):
        assert key in deploy
        assert key in workflow
    assert 'sha256sum deployment/deploy.sh' in workflow
    assert 'sha256sum deployment/backup.sh' in workflow
    assert 'sha256sum deployment/restore.sh' in workflow
    tar_start = workflow.index("tar --create --file -")
    helper_manifest_position = workflow.index(
        '--directory "$manifest_dir" image-digests.env helper-digests.env',
        tar_start,
    )
    deployment_position = workflow.index(
        '--directory "$GITHUB_WORKSPACE/deployment"',
        helper_manifest_position,
    )
    assert helper_manifest_position < deployment_position


def test_ci_starts_and_verifies_the_complete_compose_stack() -> None:
    workflow = project_text(".github/workflows/verify-and-publish.yml")

    assert "load: true" in workflow
    assert "docker compose -f deployment/compose.yml" in workflow
    assert 'up -d --wait --wait-timeout 240' in workflow
    assert (
        'chmod 644 "$secret_dir/openreviewer.crt" "$secret_dir/openreviewer.key"'
        in workflow
    )
    assert 'chmod 644 "$secret_dir/auth-users.json" \\' in workflow
    assert '"$secret_dir/github-app-private-key.pem" \\' in workflow
    assert '"$secret_dir/ai-config-key"' in workflow
    assert "OPENREVIEWER_GITHUB_ALLOWED_REPOSITORIES=lboverfys/openreviewer" in workflow
    for endpoint in (
        "/healthz",
        "/readyz",
        "/metrics",
        "http://prometheus:9090/-/ready",
        "http://alertmanager:9093/-/ready",
        "http://grafana:3000/api/health",
    ):
        assert endpoint in workflow
    assert "down --volumes --remove-orphans" in workflow


def test_failed_rollout_restores_or_stops_observability_services() -> None:
    deploy = deployment_text("deploy.sh")

    assert "rollback_services=(api worker web)" in deploy
    assert "for optional_service in prometheus alertmanager grafana" in deploy
    assert '"${failed_compose[@]}" stop "$optional_service"' in deploy
    assert '"${previous_compose[@]}" up -d --no-deps' in deploy
    assert '--scale "worker=$previous_worker_replicas"' in deploy


def test_metrics_alerts_cover_completely_missing_targets() -> None:
    rules = deployment_text("prometheus-alerts.yml")

    assert 'absent(up{job="openreviewer-api"})' in rules
    assert 'absent(up{job="openreviewer-worker"})' in rules
    assert "absent(openreviewer_workers_fresh)" in rules


def test_web_process_can_read_a_root_group_only_tls_key() -> None:
    dockerfile = project_text("web/Dockerfile")

    assert "USER nginx:root" in dockerfile


def test_python_runtime_image_applies_os_patches_and_removes_build_tools() -> None:
    dockerfile = project_text("Dockerfile")

    assert "FROM python:3.12.14-slim-bookworm" in dockerfile
    assert "apt-get upgrade -y --no-install-recommends" in dockerfile
    assert "python -m pip uninstall --yes pip setuptools" in dockerfile
