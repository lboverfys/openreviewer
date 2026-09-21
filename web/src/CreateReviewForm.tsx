import { Input } from "./components/ui/input";
import { Button } from "./components/ui/button";
import { Notice } from "./Feedback";
import { useRef, useState, type FormEvent } from "react";
import { api, ApiError } from "./api";
import { errorMessage } from "./utils";
import { WorkspaceSection } from "./Workspace";

interface CreateReviewFormProps {
  onCreated: (message: string) => void;
  onUnauthorized: () => void;
  onCancel?: () => void;
}

export default function CreateReviewForm({ onCreated, onUnauthorized, onCancel }: CreateReviewFormProps) {
  const [installationId, setInstallationId] = useState("");
  const [repositoryId, setRepositoryId] = useState("");
  const [repository, setRepository] = useState("lboverfys/NiuMa");
  const [pullRequest, setPullRequest] = useState("");
  const [headSha, setHeadSha] = useState("");
  const [message, setMessage] = useState("");
  const [failed, setFailed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const inFlight = useRef(false);
  const requestIdentity = useRef<{ body: string; key: string } | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current) return;
    inFlight.current = true;
    setSubmitting(true); setMessage(""); setFailed(false);
    const body = {
      installation_id: Number(installationId), repository_id: Number(repositoryId),
      repository: repository.trim(), pull_request_number: Number(pullRequest),
      head_sha: headSha.trim().toLowerCase(),
    };
    const fingerprint = JSON.stringify(body);
    if (requestIdentity.current?.body !== fingerprint) {
      requestIdentity.current = { body: fingerprint, key: `manual:${crypto.randomUUID()}` };
    }
    try {
      const result = await api.createReview(body, requestIdentity.current.key);
      requestIdentity.current = null;
      setPullRequest(""); setHeadSha("");
      const success = `任务 ${result.review_task_id.slice(0, 8)} 已成功调度入队`;
      setMessage(success); onCreated(success);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) { onUnauthorized(); return; }
      setFailed(true);
      setMessage(error instanceof ApiError && error.status === 422
        ? "请检查 App 安装编号、仓库编号、PR 编号和完整提交 SHA。"
        : errorMessage(error));
    } finally {
      inFlight.current = false; setSubmitting(false);
    }
  }

  return <section className="workspace-surface ws-editor" aria-label="手动审查表单">
    <div className="ws-editor-heading"><div><h2>填写 PR 信息</h2><p>用于补发或手动审查，提交后进入现有审查队列。</p></div></div>
    {message && <Notice kind={(failed ? "error" : "success")} onDismiss={() => setMessage("")}>{message}</Notice>}
    <form onSubmit={submit}><fieldset disabled={submitting}>
      <WorkspaceSection title="审查目标" description="填写真实仓库、PR 编号和需要审查的精确提交。">
        <label>目标仓库 (Owner/Repository)<Input name="repository" value={repository} onChange={event => setRepository(event.target.value)} placeholder="owner/repository" pattern={"[A-Za-z0-9_.\\-]+/[A-Za-z0-9_.\\-]+"} maxLength={255} required /></label>
        <label>Pull Request 编号<Input name="pull_request_number" type="number" min={1} step={1} value={pullRequest} onChange={event => setPullRequest(event.target.value)} required /></label>
        <label>Head Commit SHA<Input name="head_sha" className="code-font" value={headSha} onChange={event => setHeadSha(event.target.value)} minLength={40} maxLength={64} pattern="(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})" placeholder="PR 当前提交的完整 SHA" required /></label>
      </WorkspaceSection>
      <WorkspaceSection title="GitHub App 身份" description="使用已授权 App 的真实安装编号与仓库数字编号。">
        <div className="ws-form-grid"><label>Installation ID<Input name="installation_id" type="number" min={1} step={1} value={installationId} onChange={event => setInstallationId(event.target.value)} required /></label>
          <label>Repository ID<Input name="repository_id" type="number" min={1} step={1} value={repositoryId} onChange={event => setRepositoryId(event.target.value)} required /></label></div>
      </WorkspaceSection>
      <div className="ws-form-actions"><Button variant="default" type="submit" className="ws-primary">{submitting ? "正在提交…" : "提交审查任务"}</Button>{onCancel && <Button variant="outline" type="button" onClick={onCancel}>取消</Button>}<span className="ws-hint">相同内容重试会复用请求标识</span></div>
    </fieldset></form>
  </section>;
}
