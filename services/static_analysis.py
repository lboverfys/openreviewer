"""有界、纯解析的 Semgrep SARIF 2.1.0 适配器，不执行仓库脚本。"""

import json
from hashlib import sha256
from urllib.parse import unquote, urlsplit

from domain.paths import normalize_repository_path
from domain.security import redact_text
from domain.static_analysis import StaticReportUpload


def _parse(text: str, expected_sha: str):
    if len(text.encode()) > 1_000_000:
        raise ValueError("单份 SARIF 最大 1 MB")
    try:
        report = json.loads(text)
        if report["version"] != "2.1.0" or len(report["runs"]) != 1:
            raise ValueError("仅支持单个扫描运行的 SARIF 2.1.0")
        run = report["runs"][0]
        driver = run["tool"]["driver"]
        if driver["name"].casefold() != "semgrep":
            raise ValueError("当前仅支持 Semgrep SARIF")
        version = str(driver.get("semanticVersion") or driver.get("version") or "未记录")[:100]
        if any(item.get("executionSuccessful") is False for item in run.get("invocations", [])):
            raise ValueError("扫描执行失败的报告不能作为完整结果导入")
        if any(item.get("revisionId") and item["revisionId"] != expected_sha
               for item in run.get("versionControlProvenance", [])):
            raise ValueError("SARIF 记录的提交与所选提交不一致")
        raw = run.get("results", [])
        if len(raw) > 500:
            raise ValueError("单份报告最多 500 条线索，请限定扫描范围")
        results = []
        for item in raw:
            if len(item.get("locations", [])) != 1:
                raise ValueError("每条静态线索必须有一个明确的主位置")
            location = item["locations"][0]["physicalLocation"]
            uri = unquote(location["artifactLocation"]["uri"])
            if urlsplit(uri).scheme or urlsplit(uri).query or urlsplit(uri).fragment:
                raise ValueError("扫描位置必须是仓库相对路径")
            path = normalize_repository_path(uri.removeprefix("./"))
            region = location["region"]
            start = region["startLine"]
            end = region.get("endLine", start)
            if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int) or not 1 <= start <= end <= 2_147_483_647:
                raise ValueError("扫描行号无效")
            rule = str(item["ruleId"])
            if not 1 <= len(rule) <= 300:
                raise ValueError("扫描规则标识无效")
            fingerprints = item.get("partialFingerprints", {})
            stable = fingerprints.get("matchBasedId/v1")
            # 没有跨版本稳定身份时保持 unknown，不能按行号猜测新增问题。
            identity = sha256(f"{rule}:{path}:{stable}".encode()).hexdigest() if isinstance(stable, str) and 0 < len(stable) <= 512 else None
            results.append(dict(rule_id=redact_text(rule), file=path,
                start_line=start, end_line=end, level=str(item.get("level", "warning"))[:20],
                message=redact_text(str(item["message"]["text"]))[:4000],
                identity=identity, suppressed=bool(item.get("suppressions"))))
        return version, results
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("SARIF 结构无效，请导入 Semgrep 的完整报告") from exc


def parse_upload(upload: StaticReportUpload):
    version, findings = _parse(upload.head_sarif, upload.head_sha)
    baseline = None
    if upload.base_sarif is not None:
        if not upload.base_sha:
            raise ValueError("导入基线报告时必须填写基线 SHA")
        base_version, baseline = _parse(upload.base_sarif, upload.base_sha)
        if version == "未记录" or base_version != version:
            raise ValueError("基线与当前报告必须使用同一明确的 Semgrep 版本")
    elif upload.base_sha:
        raise ValueError("基线 SHA 必须同时提供对应的报告")
    known = {item["identity"] for item in baseline or [] if item["identity"]}
    for finding in findings:
        identity = finding.pop("identity")
        suppressed = finding.pop("suppressed")
        finding["baseline_state"] = ("unknown" if baseline is None or not identity or suppressed
                                     else "existing" if identity in known else "new")
    digest = sha256((upload.head_sha + (upload.base_sha or "") + upload.head_sarif + (upload.base_sarif or "")).encode()).hexdigest()
    return version, findings, digest
