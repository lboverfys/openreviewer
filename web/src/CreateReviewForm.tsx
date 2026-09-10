import { useState, type FormEvent } from "react";
import { api, ApiError } from "./api";
import { errorMessage } from "./utils";

interface CreateReviewFormProps {
  onCreated: (message: string) => void;
  onUnauthorized: () => void;
}

export default function CreateReviewForm({ onCreated, onUnauthorized }: CreateReviewFormProps) {
  const [installationId, setInstallationId] = useState("");
  const [repositoryId, setRepositoryId] = useState("");
  const [repository, setRepository] = useState("lboverfys/NiuMa");
  const [pullRequest, setPullRequest] = useState("");
  const [headSha, setHeadSha] = useState("");
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);

  function applyPreset(type: "niuma" | "demo") {
    if (type === "niuma") {
      setInstallationId("10001");
      setRepositoryId("20001");
      setRepository("lboverfys/NiuMa");
      setPullRequest("42");
      setHeadSha("a1b2c3d4e5f60718293a4b5c6d7e8f9012345678");
    } else {
      setInstallationId("10002");
      setRepositoryId("20002");
      setRepository("test-org/code-review-demo");
      setPullRequest("108");
      setHeadSha("fe98dc76ba543210fe98dc76ba543210fe98dc76");
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setMessage("");
    try {
      const result = await api.createReview(
        {
          installation_id: Number(installationId),
          repository_id: Number(repositoryId),
          repository,
          pull_request_number: Number(pullRequest),
          head_sha: headSha,
        },
        `manual:${crypto.randomUUID()}`,
      );
      setPullRequest("");
      setHeadSha("");
      const successMessage = `任务 ${result.review_task_id.slice(0, 8)} 已成功调度入队`;
      setMessage(successMessage);
      onCreated(successMessage);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        onUnauthorized();
        return;
      }
      const friendly =
        error instanceof ApiError && error.status === 422
          ? "输入内容不符合契约规范，请检查 ID 数值、仓库命名与 40 位 SHA"
          : errorMessage(error);
      setMessage(friendly);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="bento-launchpad-card" onSubmit={submit}>
      <div className="launchpad-head">
        <div className="icon-badge-warm">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4">
            <path d="M12 5v14M5 12h14" />
          </svg>
        </div>
        <div>
          <h3>手动发起审查</h3>
          <p>提交 PR 请求至 Worker 调度队列</p>
        </div>
      </div>

      {/* Quick Fill Presets */}
      <div className="preset-quick-row">
        <span className="preset-lead-tag">预设:</span>
        <button
          type="button"
          className="preset-btn"
          onClick={() => applyPreset("niuma")}
        >
          ⚡ NiuMa 主库
        </button>
        <button
          type="button"
          className="preset-btn"
          onClick={() => applyPreset("demo")}
        >
          🧪 Demo 样例
        </button>
      </div>

      <div className="launchpad-form-grid">
        <div className="two-cols-inputs">
          <label className="compact-input-control">
            <span>Installation ID</span>
            <input
              name="installation_id"
              type="number"
              min="1"
              step="1"
              placeholder="例: 10001"
              value={installationId}
              onChange={(event) => setInstallationId(event.target.value)}
              required
            />
          </label>
          <label className="compact-input-control">
            <span>Repository ID</span>
            <input
              name="repository_id"
              type="number"
              min="1"
              step="1"
              placeholder="例: 20001"
              value={repositoryId}
              onChange={(event) => setRepositoryId(event.target.value)}
              required
            />
          </label>
        </div>

        <label className="compact-input-control">
          <span>目标仓库 (Owner/Repository)</span>
          <input
            name="repository"
            value={repository}
            onChange={(event) => setRepository(event.target.value)}
            placeholder="例如: lboverfys/NiuMa"
            pattern={"[A-Za-z0-9_.\\-]+/[A-Za-z0-9_.\\-]+"}
            required
          />
        </label>

        <label className="compact-input-control">
          <span>Pull Request 编号</span>
          <input
            name="pull_request_number"
            type="number"
            min="1"
            step="1"
            placeholder="例如: 42"
            value={pullRequest}
            onChange={(event) => setPullRequest(event.target.value)}
            required
          />
        </label>

        <label className="compact-input-control">
          <span>Head Commit SHA (40位哈希)</span>
          <input
            name="head_sha"
            className="code-font"
            value={headSha}
            onChange={(event) => setHeadSha(event.target.value)}
            minLength={40}
            maxLength={64}
            pattern="[0-9a-fA-F]{40,64}"
            placeholder="40 位完整 Git 哈希"
            required
          />
        </label>
      </div>

      <button type="submit" className="warm-submit-btn" disabled={submitting}>
        {submitting ? (
          <>
            <span className="warm-btn-spinner" />
            正在排队提交…
          </>
        ) : (
          <>
            <span>提交审查任务</span>
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <path d="M5 12h14M12 5l7 7-7 7" />
            </svg>
          </>
        )}
      </button>

      {message && (
        <div className="warm-feedback-badge" role="status" aria-live="polite">
          {message}
        </div>
      )}
    </form>
  );
}
