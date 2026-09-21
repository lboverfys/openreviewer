"""评测用的隔离持久化审查样本；不执行模型或外部请求。"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from domain.enums import LocationSide
from persistence.models import (
    GitHubInstallationRecord,
    ModelCallRecord,
    ModelReviewBatchRecord,
    PullRequestVersionRecord,
    ReviewFindingRecord,
    ReviewPlanRecord,
    ReviewRunRecord,
    ReviewUnitRecord,
)


def seed_evaluation_runs(database, specs):
    now = datetime(2026, 9, 12, 10, tzinfo=UTC)
    runs, plans, calls, units, findings, batches = [], [], [], [], [], []
    versions = {}
    identities = {}
    for number, spec in enumerate(specs):
        label = spec["label"]
        run_id, plan_id, call_id = str(uuid4()), str(uuid4()), str(uuid4())
        repo_id = spec.get("repository_id", 42)
        repository = spec.get("repository", "lboverfys/NiuMa")
        head = spec.get("head_sha", "a" * 40)
        pr = spec["pr"]
        version_key = f"{repo_id}:{pr}:{head}"
        created = spec.get("created_at", now + timedelta(seconds=number))
        completed = spec.get("completed_at", created + timedelta(milliseconds=spec.get("turnaround_ms", 3000)))
        if version_key not in versions:
            versions[version_key] = {
                "id": str(uuid5(NAMESPACE_URL, "evaluation-test:" + version_key)), "review_version_key": version_key,
                "installation_id": 10, "repository_id": repo_id, "repository": repository,
                "pull_request_number": pr, "head_sha": head, "base_sha": "b" * 40,
                "title": f"评测测试 PR #{pr}", "first_seen_at": now, "last_seen_at": now,
            }
        titles = spec.get("findings", ["存在越权访问"])
        identities[label] = run_id
        runs.append({
            "id": run_id, "review_version_key": version_key, "installation_id": 10,
            "repository_id": repo_id, "repository": repository, "repository_key": repository.casefold(),
            "pull_request_number": pr, "head_sha": head, "execution_status": "completed",
            "workflow_status": "awaiting_approval", "coverage_status": spec.get("coverage", "complete"),
            "idempotency_key": run_id, "request_fingerprint": sha256(run_id.encode()).hexdigest(),
            "created_at": created, "updated_at": completed,
        })
        plans.append({
            "id": plan_id, "review_run_id": run_id, "pull_request_version_id": versions[version_key]["id"],
            "review_version_key": version_key, "head_sha": head,
            "plan_fingerprint": sha256(plan_id.encode()).hexdigest(), "planner_version": "review-planner-v4",
            "rules_complete": True, "incomplete_files": [], "rule_issues": [],
            "candidate_count": 1, "requested_candidate_count": 1, "rule_count": 0,
            "unit_count": 1, "file_count": 1, "total_estimated_input_bytes": 30,
            "model_review_completed_at": completed, "created_at": created,
        })
        unit_key = sha256(f"unit:{plan_id}".encode()).hexdigest()
        units.append({
            "id": str(uuid4()), "review_plan_id": plan_id, "ordinal": 0,
            "unit_key": unit_key, "group_key": unit_key, "file": "src/service.py",
            "blob_sha": "f" * 40, "language": "python", "patch": spec.get("patch", "@@ -1 +1 @@\n-old()\n+new()\n"),
            "patch_sha256": "f" * 64, "rule_paths": [], "review_domains": ["logic"],
            "estimated_input_bytes": 30, "planner_version": "review-planner-v4",
        })
        model = spec.get("model", "test-model")
        cost = spec.get("cost", 1000)
        duration = spec.get("model_duration_ms", 1000)
        calls.append({
            "id": call_id, "review_plan_id": plan_id, "configuration_revision": spec.get("configuration", 1),
            "provider": "openai", "api_protocol": "responses", "model": model, "status": "succeeded",
            "prompt_version": "test-prompt-v1", "request_fingerprint": "c" * 64,
            "response_status": 200, "duration_ms": duration, "input_tokens": 100, "output_tokens": 20,
            "cache_read_input_tokens": 10, "cache_write_input_tokens": 0, "reasoning_output_tokens": 0,
            "estimated_cost_microusd": cost, "finding_count": len(titles), "created_at": completed,
        })
        batches.append({
            "id": str(uuid4()), "review_plan_id": plan_id, "agent": "logic",
            "batch_number": 1, "batch_count": spec.get("batch_count", 1), "unit_keys": [unit_key],
            "estimated_input_tokens": 110, "status": "succeeded", "attempt_count": 1,
            "duration_ms": duration, "response_status": 200,
            "result": {
                "provider": "openai", "api_protocol": "responses", "model": model,
                "prompt_version": "test-prompt-v1", "status": "succeeded",
                "request_fingerprint": "c" * 64, "response_status": 200, "duration_ms": duration,
                "usage": {"input_tokens": 100, "output_tokens": 20},
                "output": {"findings": []},
                "provenance": ({"application_revision": "d" * 40, "knowledge_versions": {"rules.md": "e" * 16}}
                               if spec.get("provenance", True) else None),
            },
            "created_at": created, "updated_at": completed, "available_at": created,
        })
        for index, title in enumerate(titles):
            findings.append({
                "id": str(uuid4()), "review_run_id": run_id, "review_plan_id": plan_id,
                "model_call_id": call_id, "source_unit_key": unit_key,
                "fingerprint": sha256(f"{label}:{index}".encode()).hexdigest(),
                "head_sha": head, "severity": "high", "category": "authorization",
                "location_file": "src/service.py", "location_blob_sha": "f" * 40,
                "location_start_line": index + 1, "location_end_line": index + 1,
                "location_side": LocationSide.RIGHT.value, "location_in_diff": True,
                "title": title, "evidence": spec.get("evidence", "接口没有校验资源归属"),
                "impact": "可能读取其他用户的数据", "suggestion": "增加资源归属校验",
                "confidence": 0.9, "verification_status": "verified",
                "evidence_verification_status":spec.get("evidence_status","unverified"),
                "evidence_verification_reason":spec.get("evidence_reason","not_checked"),
                "adjudication_status": "valid", "lifecycle_status": "new",
                "created_at": created, "updated_at": completed,
            })
    with database.sessions() as session:
        dialect_insert = postgresql_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
        session.execute(dialect_insert(GitHubInstallationRecord).values(
            id=10, created_at=now, last_seen_at=now,
        ).on_conflict_do_nothing())
        session.execute(insert(ReviewRunRecord), runs)
        session.execute(dialect_insert(PullRequestVersionRecord).values(
            list(versions.values()),
        ).on_conflict_do_nothing())
        session.execute(insert(ReviewPlanRecord), plans)
        session.execute(insert(ModelCallRecord), calls)
        session.execute(insert(ReviewUnitRecord), units)
        session.execute(insert(ModelReviewBatchRecord), batches)
        if findings:
            session.execute(insert(ReviewFindingRecord), findings)
        session.commit()
    return identities
