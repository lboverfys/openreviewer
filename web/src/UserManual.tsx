import { useEffect, useRef, useState, type ReactNode } from "react";

type Topic = "dashboard" | "review" | "work" | "settings" | "knowledge" | "team" | "evaluations" | "usage" | "diagnostics";
const topics: ReadonlyArray<{ key: Topic; title: string }> = [
  { key: "dashboard", title: "第一次使用" }, { key: "review", title: "审查、取消与重新测试" },
  { key: "work", title: "核对问题与发布结果" }, { key: "settings", title: "设置模型与审查方式" },
  { key: "knowledge", title: "维护规则文档" }, { key: "team", title: "管理项目与成员" },
  { key: "evaluations", title: "检查审查效果" }, { key: "usage", title: "查看费用" },
  { key: "diagnostics", title: "任务不动时怎么办" },
];

export default function UserManual({ initialTopic, onClose }: { initialTopic: Topic; onClose: () => void }) {
  const [topic, setTopic] = useState<Topic>(initialTopic);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current!;
    const previous = document.activeElement;
    element.showModal();
    return () => { if (element.open) element.close(); if (previous instanceof HTMLElement) previous.focus(); };
  }, []);
  function link(hash: string, label: string) {
    return <a className="manual-action" href={hash} onClick={onClose}>{label} →</a>;
  }
  const content: Record<Topic, ReactNode> = {
    dashboard: <>
      <p>日常使用只需要做三件事：提交 PR、看审查结果、处理确认的问题。配置和评测由需要时再打开。</p>
      <ol>
        <li><strong>管理员先接好项目和模型。</strong>在“项目与成员”确认仓库已授权给 GitHub App，在“模型与审查设置”保存模型连接并测试。NiuMa 已接入时可以直接使用。</li>
        <li><strong>在 GitHub 提交 PR。</strong>平台收到变更后等待 CI，读取代码并执行 AI 审查。到“审查任务”查看这条 PR；页面会自动更新。</li>
        <li><strong>打开结果核对。</strong>AI 报告的是候选问题，需要结合证据判断。确认后可加入“问题处理”跟进修复。</li>
      </ol>
      <p>想先试用：打开已有任务，使用“复查此版本”。它会真实调用当前模型，另存结果，适合已合并或关闭的 PR。</p>
      {link("#", "打开审查任务")}
    </>,
    review: <>
      <ol>
        <li>在“审查任务”中搜索 PR 编号或标题，点击“查看详情”。</li>
        <li>先看当前状态和结果，再按需要展开执行过程、代码依据和日志。</li>
        <li>失败时优先“重试失败部分”；它保留已经完成的批次。“从指定阶段重试”属于高级操作，会重新计算该阶段之后的内容。</li>
      </ol>
      <dl><dt>暂停</dt><dd>暂时停止领取和继续执行，之后可以恢复。</dd>
        <dt>取消</dt><dd>结束这次任务，保留已经产生的记录。已发出的模型请求可能仍计费，取消后不再继续后续批次。</dd>
        <dt>复查此版本</dt><dd>用已经保存的代码重新调用当前模型，创建一条新记录。原任务保持原样；历史复查不重新跑 CI，也不会发布到 GitHub。</dd>
        <dt>检查最新提交</dt><dd>重新读取 PR 的最新提交，走正常审查流程。已关闭、已合并或草稿 PR 不会自动进入普通审查。</dd></dl>
      <p>“AI 已完成”表示分析完成；“待发布”表示仍需人工发布，两者不会自动等同。没有发现问题也不代表代码绝对没有缺陷。</p>
    </>,
    work: <>
      <ol>
        <li>在审查详情的“问题与结论”中查看位置、证据和影响。</li>
        <li>逐条判断为有效问题、误报、重复等，并填写依据；未完成核对的结果不能直接批准。</li>
        <li>需要修复的问题加入“问题处理”，指定负责人并跟进状态。</li>
        <li>普通 PR 结果通过审核后，先批准，再按需要手动发布到 GitHub。历史版本复查只保存在平台内。</li>
      </ol>
      <p>批准是确认平台结果，发布才会向 GitHub 写入审查结果。负责人必须有相应权限；部分失败或过时结果不能跳过校验发布。</p>
      {link("#platform?tab=work", "打开问题处理")}
    </>,
    settings: <>
      <ol>
        <li>在“AI 模型”填写服务地址、模型名称和密钥，保存后测试连接，再启用。连接测试会产生真实请求。</li>
        <li>默认让安全、规范和逻辑检查共用同一个模型；确有需要时再分别设置。</li>
        <li>在“关联代码”设置是否自动补充代码，以及是否允许向量与排序服务。关闭外部检索仍可使用关键词和代码关系检索。</li>
        <li>价格用于估算费用，实际账单以服务商为准。设置变更不会改写历史调用记录里的模型名称。</li>
      </ol>
      <p>“配置版本”用于保存一套固定的模型和规则，便于重复验证或比较。普通使用可沿用当前配置；固定版本保留保存时的规则，更新知识文档后需要另存新配置版本才会变化。</p>
      {link("#settings", "打开模型与审查设置")}
    </>,
    knowledge: <>
      <p>规则文档是 AI 可参考的业务约束，代码检索则寻找实际实现。两者用途不同。</p>
      <ol><li>选择文档阅读，点击“编辑文档”修改正文，再“保存修改”。</li>
        <li>“开启使用／暂停使用”点击后直接保存。暂停只是不再参与当前知识库检索，文档仍留在列表。</li>
        <li>不常用的文档可“移出列表”。误操作可立即“撤销移出”，也可以随时打开“已移出文档”，选择后“恢复并使用”。</li>
        <li>需要回到旧正文时，展开“历史版本”。旧内容始终保留，不用重新上传文件。</li></ol>
      <p>规则只在仓库范围和内容相关时被选中。已有审查和固定配置版本保留原快照，不会在任务中途被新文档替换。</p>
      {link("#knowledge", "打开规则文档")}
    </>,
    team: <>
      <ol><li>“项目设置”控制哪些仓库和分支接受审查、谁负责审批，以及请求和费用上限。仓库需要先授权给 GitHub App。</li>
        <li>“成员权限”用于添加账号、选择职责和限制可见仓库。只需要查看结果的人使用只读角色。</li>
        <li>修改角色、范围或密码后，成员需要重新登录。成员停用后不能继续使用原权限。</li></ol>
      <p>项目规则影响新任务，已运行的任务保留自己的配置快照。“高级限制”用于预算、外发和组织范围，日常无需逐项修改。</p>
      {link("#team", "打开项目与成员")}
    </>,
    evaluations: <>
      <p>评测用于回答“AI 找的问题对不对、有没有漏掉、改配置后有没有进步”。它不会因为打开页面就重新调用模型。</p>
      <ol><li>点击“开始新评测”，选择一份已完成的审查，再点“开始核对问题”；评测名称可以不填。</li>
        <li>阅读问题，点击“有效问题”“误报”或“暂不确定”，每次点击都会保存。详细依据在弹窗中阅读。</li>
        <li>点击“完成核对并查看统计”，一个账号就能完成。没核对和不确定的问题不会算作有效。</li>
        <li>需要比较时，再点“比较另一份审查（可选）”，选择同一 PR、同一提交的另一份完整审查；分别核对后查看两份统计。</li></ol>
      <p>已知漏报的参考清单可以另外补充；没有可靠参考时不计算找回率。这里只记录登录账号的单人判断，不声称双人独立审核。</p>
      {link("#evaluations", "打开效果评测")}
    </>,
    usage: <>
      <p>在“用量与费用”按仓库和月份查看模型请求、估算支出和预算提醒。</p>
      <dl><dt>已估算费用</dt><dd>根据服务商返回的 Token 和配置单价计算。</dd>
        <dt>待确认费用</dt><dd>请求已经发出，但还没有完整用量结果，不能当作免费。</dd>
        <dt>预算提醒</dt><dd>接近预算时提醒管理员检查；具体限制以项目设置为准。</dd></dl>
      <p>平台估算不是服务商发票。价格未填写、第三方计费不同或请求结果未知时，应先核对服务商记录。</p>
      {link("#platform?tab=usage", "打开用量与费用")}
    </>,
    diagnostics: <>
      <ol><li>先看任务状态：等待 CI 就到 GitHub 查看检查结果；已暂停需要恢复，已取消则需要新建复查。</li>
        <li>模型连接失败时，在模型设置里测试连接，核对模型名称、地址、密钥和账户余额。</li>
        <li>长期排队时，在“运行状态”检查 Worker 是否在线；失败详情和请求记录也在这里。</li>
        <li>发现配置冲突，刷新后核对自己的改动，再保存，不要连续重复提交。</li></ol>
      <p>日志中的请求 ID、时间和错误码供排查使用，不需要把密钥或完整私有配置复制到日志、知识文档或反馈中。</p>
      {link("#platform?tab=diagnostics", "打开运行状态")}
    </>,
  };
  return <dialog ref={dialog} className="user-manual" aria-labelledby="manual-title" onCancel={onClose}>
    <header><div><h2 id="manual-title">使用手册</h2><p>按你正在做的事情查看步骤。</p></div><button type="button" onClick={onClose} autoFocus>关闭</button></header>
    <div className="manual-body"><nav aria-label="手册目录">{topics.map(item =>
      <button type="button" key={item.key} aria-pressed={topic === item.key} onClick={() => setTopic(item.key)}>{item.title}</button>
    )}</nav><article><h3>{topics.find(item => item.key === topic)?.title}</h3>{content[topic]}</article></div>
  </dialog>;
}
