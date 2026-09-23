import { Table, TableHeader, TableRow, TableHead, TableBody, TableCell } from "./components/ui/table";
import { DetailDialog } from "./Feedback";
import type { DiagnosticReport } from "./types";
import { formatMoney, UsageUnknownReasons } from "./UsagePanel";

const percent = (value: number | null) => value == null ? "—" : (value * 100).toFixed(1) + "%";
const duration = (value: number | null) => value == null ? "未记录" : (value / 1000).toFixed(2) + " 秒";
const categories: Record<string, string> = {matched:"自动匹配通过",unmatched:"引用未匹配",infrastructure:"来源或基础设施异常",not_covered:"未覆盖或缺少信息",unclassified:"未分类"};

export default function ReviewInsightsPanel({data}: {data: NonNullable<DiagnosticReport["insights"]>}) {
  const request = data.requests, cost = data.completed_cost, batch = data.batches, evidence = data.evidence, cache = data.retrieval_cache, index = data.index_reuse;
  return <>
    <section className="team-card"><h2>实际请求与完成运行成本</h2>
      <p>区间创建的 HTTP 请求 {request.request_count} 次 · 2xx {request.http_2xx_count} / 非 2xx {request.http_non_2xx_count} / 尚无状态 {request.http_unknown_count}。</p>
      <p>请求 P50 / P95：{duration(request.p50_duration_ms ?? null)} / {duration(request.p95_duration_ms ?? null)}，有耗时的样本 {request.duration_sample_count} 次，含失败请求。</p>
      <p>费用已知 {request.known_count} 次 / 未知 {request.unknown_count} 次 · 等待响应 {request.reserved_count} / 待核对 {request.uncertain_count} / 已记录 {request.settled_count}。</p>
      <p><UsageUnknownReasons statistics={request} /></p>
      <p>同批已记录且费用已知的 {request.settled_priced_count} 次请求：预占 {formatMoney(request.settled_reservation_microusd)}，估算费用 {formatMoney(request.settled_cost_microusd)}。</p>
      <p>区间内完整完成 AI 审查 {cost.completed_runs} 次，其中费用完整且请求已记录 {cost.priced_runs} 次，平均估算费用 {formatMoney(cost.mean_estimated_cost_microusd)}；费用未完整确认 {cost.incomplete_cost_runs} 次，缺请求账本 {cost.missing_ledger_runs} 次。</p>
      <p>这批完成运行从创建至 AI 结果的平均耗时：{duration(cost.mean_turnaround_ms)}。</p>
      <small>均价归集同一批运行的全部关联请求，包含跨月调用。HTTP 成功、用量结算和业务审查成功分别统计。</small>
    </section>
    <section className="team-card"><h2>批次当前状态</h2>
      <p>区间内更新的 {batch.total} 个批次：等待 {batch.pending} / 执行 {batch.running} / 完成 {batch.succeeded} / 失败 {batch.failed}。</p>
      <p>实际领取且非复用批次 {batch.claimed} 个，其中重复领取 {batch.reclaimed} 个；已结束批次中的单次领取完成率 {percent(batch.terminal_claimed ? batch.single_claim_succeeded / batch.terminal_claimed : null)}（{batch.single_claim_succeeded}/{batch.terminal_claimed}）。</p>
      <small>一次领取内部仍可能有 HTTP 重试、纠错或截断拆分。当前终态和错误不能还原历史失败次数。</small>
      <DetailDialog><summary>批次当前保留的错误码</summary>{data.batch_errors.map(item => <p key={item.code}><code>{item.code}</code> · {item.count} 个批次</p>)}{data.batch_errors_truncated && <p>仅展示前 50 类错误。</p>}</DetailDialog>
    </section>
    <section className="team-card"><h2>自动引用核验</h2>
      <p>核验覆盖率 {percent(evidence.automatic_coverage)}（可匹配 {evidence.matched + evidence.unmatched} / 全部 {evidence.total}）；可匹配样本通过率 {percent(evidence.eligible_pass_rate)}（{evidence.matched}/{evidence.matched + evidence.unmatched}）。</p>
      <p>来源异常 {evidence.infrastructure} · 未覆盖/缺信息 {evidence.not_covered} · 未分类 {evidence.unclassified}，均不进入通过率分母。引用匹配只说明与源码一致，业务正确性仍须人工判断。</p>
      <DetailDialog><summary>引用核验原因</summary><Table><TableHeader><TableRow><TableHead>分类</TableHead><TableHead>原因</TableHead><TableHead>数量</TableHead></TableRow></TableHeader><TableBody>{data.evidence_reasons.map(item => <TableRow key={(item.reason ?? "none") + item.status}><TableCell>{categories[item.category] ?? item.category}</TableCell><TableCell><code>{item.reason ?? "未记录原因"}</code></TableCell><TableCell>{item.count}</TableCell></TableRow>)}</TableBody></Table>
        {data.evidence_reasons_truncated && <p>只展示前 50 组原因，以上总计仍包含全部记录。</p>}</DetailDialog>
    </section>
    <section className="team-card"><h2>复用记录</h2>
      <p>区间内创建且已完成的 {index.indexes} 个索引：新解析 {index.parsed_files} 个文件，复用解析 {index.reused_files} 个；新生成向量 {index.embedded_vectors} 个，复用向量 {index.reused_vectors} 个。</p>
      <p>按 Agent 检索组记录：查询缓存 {cache.query_all_hit_groups}/{cache.query_recorded_groups} 组全命中；精排缓存 {cache.rerank_all_hit_groups}/{cache.rerank_recorded_groups} 组全命中；共有 {cache.groups} 组记录。</p>
      <small>未全命中也可能表示未使用该缓存。这是组级标记，同一查询可能被多个角色使用，不能换算成查询命中率或避免的外部请求数。</small>
      <p>审查结果被 {batch.reused_batches} 个消费批次复用；根据历史用量估算避免输入 Token {batch.estimated_avoided_input_tokens}，另有 {batch.reused_input_unknown_batches} 个批次未记录估计值。</p>
      <small>按近期更新批次的当前保留结果统计，每个消费批次只计一次。这是输入用量估计，不能直接表示输出 Token、耗时或实际账单节省。</small>
    </section>
  </>;
}
