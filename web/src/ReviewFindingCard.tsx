import { EvidenceSnippet } from "./RetrievalTracePanel";
import type { ContextEvidence, FindingDecision, ReviewFinding } from "./types";
import { formatDate } from "./utils";

const severityLabels: Record<string, string> = {
  critical: "严重",
  high: "高风险",
  medium: "中风险",
  low: "低风险",
};

const findingLocationStatusLabels: Record<string, string> = {
  unverified: "定位：未验证",
  verified: "定位：已验证",
  rejected: "定位：无效",
};

const evidenceVerificationLabels: Record<
  ReviewFinding["evidence_verification_status"],
  string
> = {
  unverified: "源码证据：未核验",
  verified: "源码证据：已匹配",
  rejected: "源码证据：未匹配",
  not_applicable: "源码证据：不适用",
};

const findingDecisionLabels: Record<ReviewFinding["adjudication_status"], string> = {
  unreviewed: "待裁决",
  valid: "有效问题",
  false_positive: "误报",
  duplicate: "重复问题",
  out_of_scope: "超出范围",
  known_issue: "已知问题",
};

const findingDecisionOptions: ReadonlyArray<[FindingDecision, string]> = [
  ["valid", "有效问题"],
  ["false_positive", "误报"],
  ["duplicate", "重复问题"],
  ["out_of_scope", "超出范围"],
  ["known_issue", "已知问题"],
];

const findingLifecycleLabels: Record<ReviewFinding["lifecycle_status"], string> = {
  new: "本轮新增",
  still_present: "持续存在",
  reintroduced: "再次出现",
};

export default function FindingCard({
  finding,
  busy,
  editable,
  onDecision,
  contextEvidence = {},
}: {
  finding: ReviewFinding;
  contextEvidence?: Record<string, ContextEvidence>;
  busy: boolean;
  editable: boolean;
  onDecision: (finding: ReviewFinding, decision: FindingDecision) => void;
}) {
  const reviewed = finding.adjudication_status !== "unreviewed";
  return (
    <article className={`review-finding-card finding-${finding.severity}`}>
      <div className="finding-card-topline">
        <div className="finding-severity">
          <span className="finding-severity-dot" />
          {severityLabels[finding.severity] ?? finding.severity}
        </div>
        <div className="finding-card-badges">
          <span className={`finding-lifecycle lifecycle-${finding.lifecycle_status}`}>
            {findingLifecycleLabels[finding.lifecycle_status]} · 第 {finding.occurrence_count} 次
          </span>
          <span
            className={`finding-status status-${finding.location_verification_status}`}
            title="平台只校验位置是否落在当前 Diff，不代表问题事实成立"
          >
            {findingLocationStatusLabels[finding.location_verification_status]
              ?? finding.location_verification_status}
          </span>
          <span
            className={`finding-status evidence-${finding.evidence_verification_status}`}
            title="证据事实状态来自独立的人工裁决"
          >
            {evidenceVerificationLabels[finding.evidence_verification_status]}
          </span>
          <span className={`finding-status adjudication-${finding.adjudication_status}`}>
            裁决：{findingDecisionLabels[finding.adjudication_status]}
          </span>
        </div>
      </div>
      <h3>{finding.title}</h3>
      {editable && <a href={`#platform?finding=${encodeURIComponent(finding.id)}`}>加入团队待办</a>}
      <div className="finding-location">
        <code>{finding.location_file ?? "未定位到具体文件"}</code>
        {finding.location_start_line && (
          <span>
            第 {finding.location_start_line}
            {finding.location_end_line && finding.location_end_line !== finding.location_start_line
              ? `-${finding.location_end_line}`
              : ""} 行
          </span>
        )}
        <span>置信度 {Math.round(finding.confidence * 100)}%</span>
      </div>
      <div className="finding-copy-grid">
        <div><span>证据</span><p>{finding.evidence}</p></div>
        <div><span>影响</span><p>{finding.impact}</p></div>
        <div><span>建议</span><p>{finding.suggestion}</p></div>
        {finding.required_test && <div><span>建议补测</span><p>{finding.required_test}</p></div>}
      </div>
      {editable && (
        <div className="finding-actions" role="group" aria-label="人工裁决">
          {findingDecisionOptions.map(([decision, label]) => (
            <button
              type="button"
              className={`finding-decision-option ${finding.adjudication_status === decision ? "is-selected" : ""}`}
              aria-pressed={finding.adjudication_status === decision}
              disabled={busy}
              onClick={() => onDecision(finding, decision)}
              key={decision}
            >
              {label}
            </button>
          ))}
        </div>
      )}
      {reviewed && finding.reviewed_at && (
        <small className="finding-reviewed-note">由 {finding.reviewed_by ?? "管理员"} 于 {formatDate(finding.reviewed_at)} 更新</small>
      )}

      {(finding.context_references ?? []).length > 0 && <section className="finding-context-evidence">
        <h4>关联代码证据</h4>
        {(finding.context_references ?? []).map(reference => contextEvidence[reference]
          ? <EvidenceSnippet key={reference} evidence={contextEvidence[reference]} />
          : <p className="retrieval-muted" key={reference}>关联证据快照暂未加载</p>)}
      </section>}
    </article>
  );
}
