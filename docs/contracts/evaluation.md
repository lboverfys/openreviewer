# 评测契约 v1

网页端现提供[真实 PR 评测工作台](evaluation-workbench.md)，收录已完成审查的结构化结果和
变更代码快照，支持双人复核及同 PR/SHA 配对报告。它衡量审查工作流效果；下面的供应商原始
观测文件契约继续独立适用，历史结构化记录不会被伪装成原始模型响应。

## 1. 当前 CI 门禁是什么

`python -m apps.maintenance.evaluate_golden_set` 会读取 24 个小型代码改动案例和一份录制输出
夹具，再让这些固定输出经过当前生产 Prompt 组装、供应商协议解析、批次聚合、Finding 物化与
Diff 定位校验。CI 用它发现协议、解析和聚合代码的行为回归。

执行器使用 `httpx.MockTransport`，不会请求 OpenAI、Anthropic 或任何中转站。因此输出中的
precision、recall 和 F1 只描述“固定录制输出对当前夹具标签”的契约兼容性，不能称为线上模型
准确率，也不能据此比较模型能力。命令输出固定声明：

```json
{
  "execution_mode": "recorded_output_replay",
  "metric_scope": "pipeline_contract_regression",
  "real_model_accuracy": null
}
```

## 2. 门禁规则

案例 ID 在黄金集和输出夹具中必须一一对应。Finding 键按案例隔离，避免不同案例中的同名问题
相互抵消。门禁同时检查绝对 precision、recall、F1 下限，以及相对上一基线的最大退化；修改
Prompt 版本后，旧输出夹具会因 provenance 不匹配而失败，必须重新审阅并显式更新。

## 3. 什么才算真实模型评测

真实模型准确率必须来自固定版本的真实模型调用，并至少保存供应商、精确模型名、Prompt 版本、
采样时间、输入提交、原始脱敏输出和人工双人裁决。评测集应包含真实 PR、干净负样本、跨文件
问题和各风险域足量样本，并单独报告未裁决样本、定位准确率、重复率、成本和置信区间。

在这些条件满足前，项目只能把当前命令称为“录制输出流水线回归门禁”。线上行内评论仍依赖
真实人工裁决形成的仓库/风险域历史准入，不能由这 24 个夹具直接开放。

评测样本是独立的历史快照。其 `finding_id` 只作为来源引用，不是生命周期外键；即使
`review_findings` 或所属 `ReviewRun` 到达保留期被清理，样本仍必须保留，才能持续计算
仓库/风险域准入。维护循环会按 `adjudicated_at` 使用有界批次清理过期样本，默认保留 730 天，
可通过 `OPENREVIEWER_FINDING_EVALUATION_RETENTION_DAYS` 在 30 到 3650 天之间调整；保留期到期
后对应风险域可能重新变成 `insufficient_samples`，这是有意的隐私和容量边界。若要回退到旧版
级联外键，必须先确认不存在已脱离 Finding 的样本，迁移会主动拒绝不安全的降级。

## 4. 真实观测文件与报告

真实调用结果使用 [`real-evaluation.schema.json`](real-evaluation.schema.json) 校验。每个样本必须
记录仓库、PR、输入 `head_sha`、OpenReviewer 提交、供应商、精确模型名、Prompt 版本、采样时间、
Token、延迟和成本；脱敏原始输出随样本保存并绑定 SHA-256。工具会拒绝可识别的凭据和哈希不匹配
内容。原始评测数据可能包含业务信息，应保存在受控评测存储，不提交到仓库。

每个 Finding 最多记录两位不同裁决人的处置和定位结论。只有两人结论一致才进入 precision、
重复率和定位准确率；未裁决与分歧单独报告，不会被悄悄排除。若样本提供两人确认的
`expected_finding_keys`，报告同时计算 recall。运行方式：

```shell
python -m apps.maintenance.report_real_evaluation <受控目录>/real-observations.json
```

输出只含聚合统计，不回显样本、裁决人或原始模型文本；包括总体及 provider/model/prompt 分组、
precision/recall 的 95% Wilson 区间、定位准确率、重复率、未裁决数、分歧数、Token、耗时和成本。

## 检索证据协议升级

structured-review-v5 增加 context_evidence 和 context_references。现有录制回放样本不含关联上下文，保持原期望 Finding 集合；其作用仍为解析和物化回归。该夹具版本调整不代表执行过真实模型准确率评测。

检索策略评测另行保存 annotation_source、代码索引版本、模型名及固定查询集。agent_annotated 结果不能标记为 independent_human。自动标注的检索相关性不等同于真实缺陷金标。
