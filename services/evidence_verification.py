"""审查 Finding 的源码证据批量核验。

该模块只负责确定性校验，不判断业务结论是否正确。它要求 GitHub 返回的 Blob
OID 与计划中的 ``blob_sha`` 一致，再在指定行范围内寻找模型给出的证据片段；
任何无法确认的情况都返回 ``unverified``，不会为了提高发布率猜测为通过。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha1
from typing import Protocol

from domain.enums import EvidenceVerificationStatus, LocationSide
from domain.model_review import MaterializedFinding, ModelReviewInput
from domain.security import SafeApplicationError
from services.github import GitHubApiClient

_MAX_BLOB_REQUESTS = 128
_MAX_GRAPHQL_REQUEST_BYTES = 900 * 1024
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_CODE_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{2,}|\b\d+\b|[\u4e00-\u9fff]{2,}")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class EvidenceVerificationResult:
    """一条 Finding 的自动证据核验结果。"""

    status: EvidenceVerificationStatus
    reason: str
    matched_lines: tuple[int, ...] = ()


class SourceTokenProvider(Protocol):
    def get_token(self, installation_id: int) -> str: ...


class EvidenceVerifier(Protocol):
    """Worker 使用的批量证据核验边界。"""

    def verify(
        self,
        review_input: ModelReviewInput,
        findings: tuple[MaterializedFinding, ...],
        *,
        installation_id: int,
    ) -> Mapping[str, EvidenceVerificationResult]: ...


def verify_source_evidence(
    source_text: str,
    *,
    blob_sha: str,
    expected_blob_sha: str,
    start_line: int,
    end_line: int,
    side: LocationSide,
    evidence: str,
) -> EvidenceVerificationResult:
    """校验一段已回读源码是否支持 Finding 的证据描述。

    GitHub 的 Blob SHA 是 Git ``blob <byte-length>\\0<content>`` 的 SHA-1，
    因此先验证 OID，再验证行号和文本。左侧定位属于旧版本源码，当前批量回读
    器只读取 head 版本，调用方应在此之前直接标记为未验证。
    """

    if side is not LocationSide.RIGHT:
        return EvidenceVerificationResult(
            EvidenceVerificationStatus.UNVERIFIED,
            "left_side_requires_base_blob",
        )
    normalized_actual = blob_sha.strip().casefold()
    normalized_expected = expected_blob_sha.strip().casefold()
    if normalized_actual != normalized_expected:
        return EvidenceVerificationResult(
            EvidenceVerificationStatus.UNVERIFIED,
            "blob_sha_mismatch",
        )
    raw = source_text.encode("utf-8")
    git_oid = sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
    if git_oid != normalized_expected:
        return EvidenceVerificationResult(
            EvidenceVerificationStatus.UNVERIFIED,
            "source_blob_hash_mismatch",
        )
    lines = source_text.splitlines()
    if not 1 <= start_line <= end_line <= len(lines):
        return EvidenceVerificationResult(
            EvidenceVerificationStatus.UNVERIFIED,
            "line_range_out_of_bounds",
        )
    selected = tuple(lines[start_line - 1 : end_line])
    normalized_evidence = _normalize_text(evidence)
    normalized_selected = _normalize_text("\n".join(selected))
    if not normalized_evidence:
        return EvidenceVerificationResult(
            EvidenceVerificationStatus.UNVERIFIED,
            "empty_evidence",
        )
    if normalized_evidence in normalized_selected:
        return EvidenceVerificationResult(
            EvidenceVerificationStatus.VERIFIED,
            "exact_text_match",
            tuple(range(start_line, end_line + 1)),
        )

    evidence_tokens = set(_CODE_TOKEN.findall(normalized_evidence.casefold()))
    source_tokens = set(_CODE_TOKEN.findall(normalized_selected.casefold()))
    # 自然语言证据通常会带解释文字，不能要求整段完全相等；至少要命中两个
    # 稳定代码标识符，且命中比例达到一半，避免“用户、服务、问题”等通用词误通过。
    if len(evidence_tokens) >= 2:
        matched = evidence_tokens & source_tokens
        if len(matched) >= 2 and len(matched) * 2 >= len(evidence_tokens):
            return EvidenceVerificationResult(
                EvidenceVerificationStatus.VERIFIED,
                "identifier_overlap",
                tuple(range(start_line, end_line + 1)),
            )
    return EvidenceVerificationResult(
        EvidenceVerificationStatus.UNVERIFIED,
        "evidence_text_not_found",
    )


class GitHubEvidenceVerifier:
    """用一次有界 GraphQL 请求回读一批 head Blob。"""

    def __init__(
        self,
        api: GitHubApiClient,
        tokens: SourceTokenProvider,
        *,
        max_blobs: int = _MAX_BLOB_REQUESTS,
    ) -> None:
        if not 1 <= max_blobs <= _MAX_BLOB_REQUESTS:
            raise ValueError("evidence verifier blob limit is invalid")
        self._api = api
        self._tokens = tokens
        self._max_blobs = max_blobs

    def verify(
        self,
        review_input: ModelReviewInput,
        findings: tuple[MaterializedFinding, ...],
        *,
        installation_id: int | None = None,
    ) -> dict[str, EvidenceVerificationResult]:
        """返回按 Finding 指纹索引的核验结果；网络失败时全部安全降级。"""

        results: dict[str, EvidenceVerificationResult] = {}
        if installation_id is None or installation_id <= 0:
            return {
                finding_item.finding.fingerprint: EvidenceVerificationResult(
                    EvidenceVerificationStatus.UNVERIFIED,
                    "installation_id_missing",
                )
                for finding_item in findings
            }
        candidates: list[tuple[MaterializedFinding, str, int, int, LocationSide]] = []
        units = {unit.unit_key: unit for unit in review_input.units}
        for materialized in findings:
            location = materialized.finding.location
            if location is None:
                results[materialized.finding.fingerprint] = EvidenceVerificationResult(
                    EvidenceVerificationStatus.UNVERIFIED,
                    "finding_has_no_location",
                )
                continue
            if location.side is not LocationSide.RIGHT:
                results[materialized.finding.fingerprint] = EvidenceVerificationResult(
                    EvidenceVerificationStatus.UNVERIFIED,
                    "left_side_requires_base_blob",
                )
                continue
            if materialized.source_unit_key not in units:
                results[materialized.finding.fingerprint] = EvidenceVerificationResult(
                    EvidenceVerificationStatus.UNVERIFIED,
                    "source_unit_missing",
                )
                continue
            candidates.append(
                (
                    materialized,
                    location.file,
                    location.start_line,
                    location.end_line,
                    location.side,
                )
            )
        if not candidates:
            return results

        blob_keys = {
            (entry[1], units[entry[0].source_unit_key].blob_sha)
            for entry in candidates
        }
        if len(blob_keys) > self._max_blobs:
            allowed = set(sorted(blob_keys)[: self._max_blobs])
            for entry in candidates:
                key = (
                    entry[1],
                    units[entry[0].source_unit_key].blob_sha,
                )
                if key not in allowed:
                    results[
                        entry[0].finding.fingerprint
                    ] = EvidenceVerificationResult(
                        EvidenceVerificationStatus.UNVERIFIED,
                        "blob_batch_limit",
                    )
            candidates = [
                entry
                for entry in candidates
                if (
                    entry[1],
                    units[entry[0].source_unit_key].blob_sha,
                )
                in allowed
            ]
        if not candidates:
            return results

        try:
            blobs = self._load_blobs(
                review_input.repository,
                review_input.repository_id,
                review_input.head_sha,
                tuple(
                    sorted(
                        {
                            (
                                path,
                                units[materialized_item.source_unit_key].blob_sha,
                            )
                            for materialized_item, path, _start, _end, _side in candidates
                        }
                    )
                ),
                self._tokens.get_token(installation_id),
            )
        except (SafeApplicationError, ValueError, KeyError, TypeError):
            for failed_entry, _path, _start, _end, _side in candidates:
                results[
                    failed_entry.finding.fingerprint
                ] = EvidenceVerificationResult(
                    EvidenceVerificationStatus.UNVERIFIED,
                    "source_readback_failed",
                )
            return results

        for checked_entry, path, start, end, side in candidates:
            unit = units[checked_entry.source_unit_key]
            blob = blobs.get((path, unit.blob_sha))
            if blob is None:
                results[
                    checked_entry.finding.fingerprint
                ] = EvidenceVerificationResult(
                    EvidenceVerificationStatus.UNVERIFIED,
                    "source_blob_unavailable",
                )
                continue
            results[checked_entry.finding.fingerprint] = verify_source_evidence(
                blob,
                blob_sha=unit.blob_sha,
                expected_blob_sha=unit.blob_sha,
                start_line=start,
                end_line=end,
                side=side,
                evidence=checked_entry.finding.evidence,
            )
        return results

    def _load_blobs(
        self,
        repository: str,
        repository_id: int,
        head_sha: str,
        specs: tuple[tuple[str, str], ...],
        token: str,
    ) -> dict[tuple[str, str], str]:
        owner, name = repository.split("/", 1)
        variables: dict[str, str] = {"owner": owner, "name": name}
        declarations = ["$owner: String!", "$name: String!"]
        selections: list[str] = []
        aliases: dict[str, tuple[str, str]] = {}
        for index, (path, blob_sha) in enumerate(specs):
            alias = f"blob{index}"
            variable = f"expression{index}"
            declarations.append(f"${variable}: String!")
            variables[variable] = f"{head_sha}:{path}"
            selections.append(
                f"{alias}: object(expression: ${variable}) {{ __typename "
                "... on Blob { oid byteSize isBinary text } }"
            )
            aliases[alias] = (path, blob_sha.casefold())
        query = (
            f"query SourceBlobs({', '.join(declarations)}) {{ repository(owner: $owner, name: $name) {{ "
            "databaseId nameWithOwner "
            + " ".join(selections)
            + " } }"
        )
        body = {"query": query, "variables": variables}
        # 防止极端路径集合把 GraphQL 请求推过网关限制；超限时调用方会把
        # Finding 留在 unverified，而不是拆成逐条 HTTP 请求。
        if len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > _MAX_GRAPHQL_REQUEST_BYTES:
            raise ValueError("evidence GraphQL request is too large")
        payload = self._api.request_json(
            "POST",
            "/graphql",
            bearer_token=token,
            json_body=body,
            max_response_bytes=_MAX_SOURCE_BYTES * 2,
        ).payload
        if not isinstance(payload, dict) or payload.get("errors"):
            raise ValueError("GitHub source Blob response is incomplete")
        data = payload.get("data")
        repository_payload = data.get("repository") if isinstance(data, dict) else None
        if not isinstance(repository_payload, dict):
            raise ValueError("GitHub source repository response is invalid")
        name_with_owner = repository_payload.get("nameWithOwner")
        if (
            repository_payload.get("databaseId") != repository_id
            or not isinstance(name_with_owner, str)
            or name_with_owner.casefold() != repository.casefold()
        ):
            raise ValueError("GitHub source repository identity mismatch")
        result: dict[tuple[str, str], str] = {}
        for alias, key in aliases.items():
            raw = repository_payload.get(alias)
            if not isinstance(raw, dict) or raw.get("__typename") != "Blob":
                continue
            oid = raw.get("oid")
            size = raw.get("byteSize")
            text = raw.get("text")
            if (
                not isinstance(oid, str)
                or oid.casefold() != key[1]
                or not isinstance(size, int)
                or size < 0
                or size > _MAX_SOURCE_BYTES
                or raw.get("isBinary") is not False
                or not isinstance(text, str)
            ):
                continue
            result[key] = text
        return result


def _normalize_text(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def apply_evidence_verification(
    findings: tuple[MaterializedFinding, ...],
    results: Mapping[str, EvidenceVerificationResult],
) -> tuple[MaterializedFinding, ...]:
    """把核验器结果写回 Finding；缺失结果一律保持未核验。"""

    updated: list[MaterializedFinding] = []
    for item in findings:
        result = results.get(item.finding.fingerprint)
        if result is None:
            status = EvidenceVerificationStatus.UNVERIFIED
            reason = "verification_result_missing"
        else:
            status = EvidenceVerificationStatus(result.status)
            reason = str(result.reason).strip()[:120] or "verification_reason_missing"
        updated.append(
            MaterializedFinding(
                source_unit_key=item.source_unit_key,
                finding=item.finding.model_copy(
                    update={
                        "evidence_verification_status": status,
                        "evidence_verification_reason": reason,
                    }
                ),
            )
        )
    return tuple(updated)
