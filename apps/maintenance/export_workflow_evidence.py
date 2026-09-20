"""显式归档工作流证据并以原有报告算法复算；不会发起模型请求。"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from sqlalchemy import DateTime, func, insert, select

from domain.evaluation_workbench import (
    EvaluationBallot,
    EvaluationNotFoundError,
    EvaluationSource,
    ReferenceDefect,
    ReferenceReview,
    assessment_metrics,
    reference_status,
)
from persistence.database import Database
from persistence.evaluation_outputs import output_scope
from persistence.evaluation_reports import comparison_report
from persistence.models import (
    Base,
    EvaluationCaseRecord,
    EvaluationDatasetRecord,
    EvaluationModelOutputRecord,
    EvaluationObservationRecord,
    ModelUsageRequestRecord,
    ReviewRunRecord,
)
from persistence.resource_scope import resource_predicate
from services.evaluation_workbench import EvaluationWorkbench
from services.rbac import ResourceScope


def controlled_path(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("证据目录必须使用显式绝对路径")
    resolved = path.resolve()
    if os.name == "nt" and resolved.drive.casefold() != "d:":
        raise ValueError("Windows 受控证据和复算临时文件必须位于 D 盘")
    if any((parent / ".git").exists() for parent in (resolved, *resolved.parents)):
        raise ValueError("受控原始证据不得写入 Git 工作区")
    return resolved


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("归档包含不支持的值类型")


def _encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=_json_default).encode("utf-8")


def export_archive(sessions, dataset_id: str, output: Path, scope: ResourceScope, *, run_ids: tuple[str, ...] = ()) -> dict[str, Any]:
    output = controlled_path(output)
    if output.exists():
        raise ValueError("归档目录已存在，拒绝覆盖历史交付")
    service = EvaluationWorkbench(sessions)
    service.dataset(dataset_id, scope)
    report = service.report(dataset_id, scope)
    now = datetime.now(UTC)
    with sessions() as session:
        dataset = dict(session.execute(select(*EvaluationDatasetRecord.__table__.columns)
            .where(EvaluationDatasetRecord.id == dataset_id)).mappings().one())
        cases = [dict(row) for row in session.execute(select(*EvaluationCaseRecord.__table__.columns)
            .where(EvaluationCaseRecord.dataset_id == dataset_id).order_by(EvaluationCaseRecord.id).limit(201)).mappings()]
        if len(cases) > 200:
            raise ValueError("评测样本超出归档边界")
        observation_ids = session.execute(select(EvaluationObservationRecord.id, EvaluationObservationRecord.source_run_id)
            .join(EvaluationCaseRecord, EvaluationCaseRecord.id == EvaluationObservationRecord.case_id)
            .where(EvaluationCaseRecord.dataset_id == dataset_id).order_by(EvaluationObservationRecord.id).limit(401)).all()
        if len(observation_ids) > 400:
            raise ValueError("评测观察超出归档边界")
        selected = tuple(sorted(set(run_ids) | {row.source_run_id for row in observation_ids}))
        if not selected or len(selected) > 400:
            raise ValueError("归档必须包含 1 至 400 个计划运行，包含失败和未纳入运行")
        runs = [dict(row) for row in session.execute(select(
            ReviewRunRecord.id, ReviewRunRecord.repository, ReviewRunRecord.pull_request_number,
            ReviewRunRecord.head_sha, ReviewRunRecord.execution_status, ReviewRunRecord.coverage_status,
            ReviewRunRecord.capture_model_outputs, ReviewRunRecord.repository_policy,
        ).where(ReviewRunRecord.id.in_(selected), resource_predicate(scope,
            installation_column=ReviewRunRecord.installation_id, repository_column=ReviewRunRecord.repository,
            repository_key_column=ReviewRunRecord.repository_key))).mappings()]
        available = {row["id"] for row in runs} | {row.source_run_id for row in observation_ids}
        if set(selected) - available:
            raise EvaluationNotFoundError("部分计划运行不存在或无权访问")
        usage = [dict(row) for row in session.execute(select(
            ModelUsageRequestRecord.review_run_id,
            func.count().label("request_count"),
            func.sum(ModelUsageRequestRecord.estimated_cost_microusd).label("known_estimated_cost_microusd"),
            func.count().filter(ModelUsageRequestRecord.estimated_cost_microusd.is_(None)).label("unknown_cost_count"),
            func.count().filter(ModelUsageRequestRecord.status == "uncertain").label("uncertain_count"),
        ).where(ModelUsageRequestRecord.review_run_id.in_(selected), resource_predicate(scope,
            installation_column=ModelUsageRequestRecord.installation_id, repository_column=ModelUsageRequestRecord.repository,
            repository_key_column=ModelUsageRequestRecord.repository_key)).group_by(ModelUsageRequestRecord.review_run_id)).mappings()]
    output.mkdir(parents=True, exist_ok=False)
    files: list[dict[str, Any]] = []

    def save(name: str, kind: str, value: object):
        if len(files) >= 20_000:
            raise ValueError("单份归档超过 20000 个文件，请拆分评测批次；当前目录未完成")
        content = _encoded(value)
        with (output / name).open("xb") as handle:
            handle.write(content)
        files.append({"name":name, "kind":kind, "sha256":sha256(content).hexdigest(), "byte_size":len(content)})

    save("dataset.json", "dataset", dataset)
    save("cases.json", "cases", cases)
    save("report.json", "report", report.model_dump(mode="json"))
    ids = [row.id for row in observation_ids]
    for start in range(0, len(ids), 10):
        with sessions() as session:
            rows = session.execute(select(*EvaluationObservationRecord.__table__.columns)
                .where(EvaluationObservationRecord.id.in_(ids[start:start + 10])).order_by(EvaluationObservationRecord.id)).mappings().all()
        for row in rows:
            save("observation-" + sha256(row["id"].encode()).hexdigest() + ".json", "observation", dict(row))
    last = ""
    capture_counts: dict[str, int] = {}
    while True:
        with sessions() as session:
            rows = session.execute(select(*EvaluationModelOutputRecord.__table__.columns).where(
                EvaluationModelOutputRecord.review_run_id.in_(selected), EvaluationModelOutputRecord.id > last,
                output_scope(scope),
            ).order_by(EvaluationModelOutputRecord.id).limit(25)).mappings().all()
        if not rows:
            break
        for row in rows:
            value = dict(row)
            expiry = value["expires_at"]
            if (expiry.replace(tzinfo=UTC) if expiry.tzinfo is None else expiry) <= now:
                value.update(status="expired", output_text=None)
            capture_counts[value["status"]] = capture_counts.get(value["status"], 0) + 1
            save("call-" + sha256(value["id"].encode()).hexdigest() + ".json", "call", value)
        last = rows[-1]["id"]
    if service.report(dataset_id, scope).data_version != report.data_version:
        raise ValueError("归档期间人工评测发生变化；当前目录不完整，请重新导出到新目录")
    manifest = {
        "schema_version":1, "metric_scope":"paired_review_workflow", "generated_at":now,
        "data_version":report.data_version, "review_mode":report.review_mode,
        "planned_run_ids":selected, "runs":runs, "usage":usage, "capture_status_counts":capture_counts,
        "sample_flow":{
            "planned_runs":len(selected), "workflow_snapshots":len(observation_ids),
            "failed_or_interrupted_runs":sum(row["execution_status"] in {"failed", "timed_out", "cancelled"} for row in runs),
            "not_in_workbench_runs":len(set(selected) - {row.source_run_id for row in observation_ids}),
            "source_removed_runs":len(set(selected) - {row["id"] for row in runs}),
            "quality_pairs":report.quality_pairs, "reference_pairs":report.reference_pairs,
        },
        "files":files,
        "limitations":["受控归档可能含业务源码和复核身份，不得直接公开", "最终问题只保存在工作流观察中，不复制给每次模型调用", "未显式列入的计划失败运行无法由成功样本反推，调用方应传入完整计划清单", "没有供应商正文的历史运行不会被补造为完整原始证据"],
    }
    # 清单最后落盘；没有清单的目录是失败或中断归档，不视为成功。
    with (output / "manifest.json").open("xb") as handle:
        handle.write(_encoded(manifest))
    return manifest


def _restore_dates(record, values):
    return {key: datetime.fromisoformat(value) if value is not None and isinstance(record.__table__.columns[key].type, DateTime) else value
            for key, value in values.items()}


def verify_archive(archive: Path, work_directory: Path) -> dict[str, Any]:
    archive = controlled_path(archive)
    work_directory = controlled_path(work_directory)
    manifest = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
    if manifest["schema_version"] != 1 or len(manifest["files"]) > 20_000:
        raise ValueError("归档版本或文件数量无效")
    metadata = {}
    observations = []
    for entry in manifest["files"]:
        path = archive / entry["name"]
        if Path(entry["name"]).name != entry["name"] or path.is_symlink() or not path.resolve().is_relative_to(archive):
            raise ValueError("归档文件路径越界")
        if path.stat().st_size != entry["byte_size"] or entry["byte_size"] > 16 * 1024 * 1024:
            raise ValueError("归档文件大小不匹配或超过单文件边界")
        content = path.read_bytes()
        if sha256(content).hexdigest() != entry["sha256"]:
            raise ValueError("归档文件哈希不匹配")
        if entry["kind"] == "observation":
            observations.append(path)
        elif entry["kind"] != "call":
            metadata[entry["kind"]] = json.loads(content)
    dataset = metadata["dataset"]
    cases = {row["id"]: row for row in metadata["cases"]}
    if len(cases) > 200 or len(observations) > 400:
        raise ValueError("归档样本超出工作台边界")
    work_directory.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="workflow-evidence-", dir=work_directory) as temp:
        database = Database.connect("sqlite:///" + (Path(temp) / "replay.sqlite3").as_posix())
        try:
            Base.metadata.create_all(database.engine, tables=[Base.metadata.tables[record.__tablename__] for record in (
                EvaluationDatasetRecord, EvaluationCaseRecord, EvaluationObservationRecord,
            )])
            with database.sessions() as session, session.begin():
                session.execute(insert(EvaluationDatasetRecord), [_restore_dates(EvaluationDatasetRecord, dataset)])
                for case in cases.values():
                    case["reference_status"] = reference_status(tuple(ReferenceReview.model_validate(value) for value in case["reference_reviews"]), dataset["review_mode"])
                if cases:
                    session.execute(insert(EvaluationCaseRecord), [_restore_dates(EvaluationCaseRecord, case) for case in cases.values()])
            pending_rows = []
            for path in observations:
                row = json.loads(path.read_text(encoding="utf-8"))
                if sha256(_encoded(row["source_snapshot"])).hexdigest() != row["snapshot_sha256"]:
                    raise ValueError("工作流来源快照哈希不匹配")
                source = EvaluationSource.model_validate(row["source_snapshot"])
                refs = cases[row["case_id"]]["reference_defects"]
                state, metrics = assessment_metrics(source.findings,
                    tuple(EvaluationBallot.model_validate(value) for value in row["ballots"]),
                    tuple(ReferenceDefect.model_validate(value) for value in refs) if refs is not None else None,
                    dataset["review_mode"])
                row.update(assessment_status=state, metrics=metrics, source_snapshot={}, ballots=[])
                pending_rows.append(_restore_dates(EvaluationObservationRecord, row))
                if len(pending_rows) == 10:
                    with database.sessions() as session, session.begin():
                        session.execute(insert(EvaluationObservationRecord), pending_rows)
                    pending_rows = []
            if pending_rows:
                with database.sessions() as session, session.begin():
                    session.execute(insert(EvaluationObservationRecord), pending_rows)
            with database.sessions() as session:
                recalculated = comparison_report(session, dataset["id"], ResourceScope.unrestricted_scope(), "validation").model_dump(mode="json", exclude={"generated_at"})
            expected = {key:value for key, value in metadata["report"].items() if key != "generated_at"}
            if recalculated != expected:
                raise ValueError("归档复算与原报告不一致；请核对数据与程序版本")
            return recalculated
        finally:
            database.engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--dataset-id", required=True)
    export.add_argument("--repository", required=True)
    export.add_argument("--installation-id", type=int, required=True)
    export.add_argument("--run-id", action="append", default=[])
    export.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--archive", type=Path, required=True)
    verify.add_argument("--work-directory", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "verify":
        report = verify_archive(args.archive, args.work_directory)
        print(json.dumps({"verified":True, "data_version":report["data_version"]}))
        return
    scope = ResourceScope(installation_ids=frozenset({args.installation_id}), repositories=frozenset({args.repository}))
    database = Database.from_environment()
    try:
        manifest = export_archive(database.sessions, args.dataset_id, args.output, scope, run_ids=tuple(args.run_id))
        print(json.dumps({"output":str(args.output), "sample_flow":manifest["sample_flow"]}))
    finally:
        database.engine.dispose()


if __name__ == "__main__":
    main()
