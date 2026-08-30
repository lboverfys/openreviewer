import { ChangeEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";

import { api, ApiError } from "./api";
import type {
  AuthUser,
  KnowledgeCitation,
  KnowledgeDocument,
  KnowledgeLibrary,
  KnowledgeMutation,
} from "./types";
import { errorMessage, formatDate } from "./utils";

interface KnowledgePageProps {
  user: AuthUser;
  onBack: () => void;
  onOpenSettings: () => void;
  onSignedOut: (message?: string) => void;
}

interface DocumentDraft {
  source: string;
  content: string;
  enabled: boolean;
}

const EMPTY_DRAFT: DocumentDraft = {
  source: "new-rule.md",
  content: "# 新规则\n\n在这里填写审查规则。\n",
  enabled: false,
};

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KiB`;
}

function documentDraft(document: KnowledgeDocument): DocumentDraft {
  return {
    source: document.source,
    content: document.content,
    enabled: document.enabled,
  };
}

export default function KnowledgePage({
  user,
  onBack,
  onOpenSettings,
  onSignedOut,
}: KnowledgePageProps) {
  const [library, setLibrary] = useState<KnowledgeLibrary | null>(null);
  const [document, setDocument] = useState<KnowledgeDocument | null>(null);
  const [draft, setDraft] = useState<DocumentDraft>(EMPTY_DRAFT);
  const [creating, setCreating] = useState(false);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [listQuery, setListQuery] = useState("");
  const [editorMode, setEditorMode] = useState<"edit" | "preview">("edit");
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [messageKind, setMessageKind] = useState<"success" | "error">("success");
  const [searchQuery, setSearchQuery] = useState("");
  const [citations, setCitations] = useState<KnowledgeCitation[]>([]);
  const uploadRef = useRef<HTMLInputElement>(null);

  const dirty = creating
    ? draft.source !== EMPTY_DRAFT.source
      || draft.content !== EMPTY_DRAFT.content
      || draft.enabled !== EMPTY_DRAFT.enabled
    : Boolean(document && (
      draft.source !== document.source
      || draft.content !== document.content
      || draft.enabled !== document.enabled
    ));

  const handleError = useCallback((reason: unknown) => {
    if (reason instanceof ApiError && reason.status === 401) {
      onSignedOut("登录状态已失效，请重新登录");
      return;
    }
    setMessageKind("error");
    setMessage(errorMessage(reason));
  }, [onSignedOut]);

  const openDocument = useCallback(async (
    documentId: string,
    clearFeedback = true,
    signal?: AbortSignal,
  ) => {
    setBusy("open");
    try {
      const next = await api.knowledgeDocument(documentId, signal);
      setDocument(next);
      setDraft(documentDraft(next));
      setCreating(false);
      if (clearFeedback) setMessage("");
    } catch (reason) {
      if (signal?.aborted) return;
      handleError(reason);
    } finally {
      if (!signal?.aborted) setBusy("");
    }
  }, [handleError]);

  const refreshLibrary = useCallback(async (
    preferredId?: string,
    signal?: AbortSignal,
  ) => {
    setBusy((current) => current || "refresh");
    try {
      const next = await api.knowledgeDocuments(includeArchived, signal);
      setLibrary(next);
      const target = preferredId
        ? next.items.find((item) => item.id === preferredId)
        : next.items.find((item) => item.id === document?.id) ?? next.items[0];
      if (target) {
        await openDocument(target.id, false, signal);
      } else {
        setDocument(null);
      }
    } catch (reason) {
      if (signal?.aborted) return;
      handleError(reason);
    } finally {
      if (!signal?.aborted) setBusy("");
    }
  }, [document?.id, handleError, includeArchived, openDocument]);

  useEffect(() => {
    const controller = new AbortController();
    void refreshLibrary(undefined, controller.signal);
    return () => controller.abort();
  }, [includeArchived]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!message || messageKind !== "success") return undefined;
    const timer = window.setTimeout(() => setMessage(""), 4000);
    return () => window.clearTimeout(timer);
  }, [message, messageKind]);

  useEffect(() => {
    function warnBeforeUnload(event: BeforeUnloadEvent) {
      if (!dirty) return;
      event.preventDefault();
    }
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [dirty]);

  const visibleItems = useMemo(() => {
    const query = listQuery.trim().toLocaleLowerCase();
    if (!query) return library?.items ?? [];
    return (library?.items ?? []).filter((item) => (
      item.title.toLocaleLowerCase().includes(query)
      || item.source.toLocaleLowerCase().includes(query)
    ));
  }, [library, listQuery]);

  function applyMutation(result: KnowledgeMutation, successMessage: string) {
    setDocument(result.document);
    setDraft(documentDraft(result.document));
    setCreating(false);
    setMessageKind("success");
    setMessage(successMessage);
    void refreshLibrary(result.document.id);
  }

  async function saveDocument() {
    if (!library) return;
    setBusy("save");
    setMessage("");
    try {
      const result = creating
        ? await api.createKnowledgeDocument({
          expected_revision: library.revision,
          source: draft.source,
          content: draft.content,
          enabled: draft.enabled,
        })
        : await api.updateKnowledgeDocument(document!.id, {
          expected_revision: library.revision,
          expected_document_version: document!.current_version,
          source: draft.source,
          content: draft.content,
          enabled: draft.enabled,
        });
      applyMutation(result, creating ? "知识文档已创建" : "知识文档已保存");
    } catch (reason) {
      handleError(reason);
      if (reason instanceof ApiError && reason.status === 409) {
        await refreshLibrary(document?.id);
      }
    } finally {
      setBusy("");
    }
  }

  async function setArchived(archived: boolean) {
    if (!library || !document) return;
    if (archived && !window.confirm("归档后文档不会参与新的审查，确定继续吗？")) return;
    setBusy(archived ? "archive" : "restore");
    try {
      const result = await api.setKnowledgeDocumentArchived(
        document.id,
        archived,
        library.revision,
        document.current_version,
      );
      applyMutation(result, archived ? "知识文档已归档" : "知识文档已恢复，默认保持停用");
    } catch (reason) {
      handleError(reason);
    } finally {
      setBusy("");
    }
  }

  async function restoreVersion(version: number) {
    if (!library || !document || version === document.current_version) return;
    if (!window.confirm(`将第 ${version} 版内容恢复为一个新版本，确定继续吗？`)) return;
    setBusy(`version-${version}`);
    try {
      const result = await api.restoreKnowledgeVersion(
        document.id,
        version,
        library.revision,
        document.current_version,
      );
      applyMutation(result, `已从第 ${version} 版恢复并生成新版本`);
    } catch (reason) {
      handleError(reason);
    } finally {
      setBusy("");
    }
  }

  async function testSearch() {
    if (!searchQuery.trim()) return;
    setBusy("search");
    try {
      setCitations((await api.searchKnowledge(searchQuery.trim(), 8)).items);
    } catch (reason) {
      handleError(reason);
    } finally {
      setBusy("");
    }
  }

  async function uploadMarkdown(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    if (dirty && !window.confirm("当前修改尚未保存，仍要导入其他文档吗？")) return;
    if (!file.name.toLocaleLowerCase().endsWith(".md") || file.size > 512 * 1024) {
      setMessageKind("error");
      setMessage("请选择不超过 512 KiB 的 .md 文件");
      return;
    }
    try {
      const content = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
      setCreating(true);
      setDocument(null);
      setDraft({ source: file.name, content, enabled: false });
      setEditorMode("edit");
      setMessage("");
    } catch {
      setMessageKind("error");
      setMessage("文件不是可读取的 UTF-8 Markdown");
    }
  }

  function startNewDocument() {
    if (dirty && !window.confirm("当前修改尚未保存，仍要新建文档吗？")) return;
    setCreating(true);
    setDocument(null);
    setDraft(EMPTY_DRAFT);
    setEditorMode("edit");
    setMessage("");
  }

  return (
    <div className="knowledge-page">
      <header className="knowledge-navbar">
        <button type="button" className="knowledge-icon-btn" onClick={onBack} title="返回审查控制台" aria-label="返回审查控制台">←</button>
        <div className="knowledge-brand"><strong>知识库</strong><span>OpenReviewer / Markdown RAG</span></div>
        <div className="knowledge-nav-actions">
          <button type="button" className="knowledge-nav-btn" onClick={onOpenSettings}>AI 设置</button>
          <span className="knowledge-user">{user.username}</span>
        </div>
      </header>

      <main className="knowledge-main">
        <section className="knowledge-titlebar">
          <div><span className="knowledge-eyebrow">RAG KNOWLEDGE BASE</span><h1>审查知识文档</h1><p>维护新的审查规则时会生成不可变版本，已开始的任务继续使用原知识快照。</p></div>
          <div className="knowledge-stats">
            <span><b>{library?.enabled_count ?? 0}</b>启用</span>
            <span><b>{library?.total ?? 0}</b>文档</span>
            <span><b>{formatSize(library?.total_enabled_bytes ?? 0)}</b>启用内容</span>
            <span><b>r{library?.revision ?? 0}</b>版本</span>
          </div>
        </section>

        <div className="knowledge-workspace">
          <aside className="knowledge-document-pane">
            <div className="knowledge-pane-heading"><div><strong>文档</strong><small>{visibleItems.length} 条</small></div><button type="button" onClick={startNewDocument} title="新建 Markdown 文档">＋</button></div>
            <div className="knowledge-list-tools">
              <input id="knowledge-list-query" name="knowledge-list-query" value={listQuery} onChange={(event) => setListQuery(event.target.value)} placeholder="搜索名称或路径" />
              <label><input id="knowledge-include-archived" name="knowledge-include-archived" type="checkbox" checked={includeArchived} onChange={(event) => setIncludeArchived(event.target.checked)} />显示归档</label>
            </div>
            <div className="knowledge-document-list">
              {visibleItems.map((item) => <button key={item.id} type="button" className={`${document?.id === item.id ? "is-selected" : ""} ${item.archived ? "is-archived" : ""}`} onClick={() => void openDocument(item.id)}><span><strong>{item.title}</strong><small>{item.source}</small></span><i className={item.enabled ? "is-enabled" : ""}>{item.archived ? "归档" : item.enabled ? "启用" : "停用"}</i></button>)}
              {!busy && visibleItems.length === 0 && <div className="knowledge-list-empty">没有符合条件的文档</div>}
            </div>
            <div className="knowledge-list-actions">
              <button type="button" onClick={() => uploadRef.current?.click()}>上传 .md</button>
              <input ref={uploadRef} id="knowledge-upload" name="knowledge-upload" type="file" accept=".md,text/markdown,text/plain" hidden onChange={(event) => void uploadMarkdown(event)} />
            </div>
          </aside>

          <section className="knowledge-editor-pane">
            {creating || document ? <>
              <div className="knowledge-editor-heading">
                <label><span>文档路径</span><input id="knowledge-source" name="knowledge-source" value={draft.source} maxLength={200} disabled={Boolean(busy) || Boolean(document?.archived)} onChange={(event) => setDraft((current) => ({ ...current, source: event.target.value }))} /></label>
                <div className="knowledge-editor-status"><label><input id="knowledge-enabled" name="knowledge-enabled" type="checkbox" checked={draft.enabled} disabled={Boolean(busy) || Boolean(document?.archived)} onChange={(event) => setDraft((current) => ({ ...current, enabled: event.target.checked }))} />参与审查</label><span>{creating ? "新文档" : `第 ${document?.current_version} 版`}</span></div>
              </div>
              <div className="knowledge-editor-tabs"><button type="button" className={editorMode === "edit" ? "is-active" : ""} onClick={() => setEditorMode("edit")}>编辑</button><button type="button" className={editorMode === "preview" ? "is-active" : ""} onClick={() => setEditorMode("preview")}>预览</button><small>{formatSize(new TextEncoder().encode(draft.content).length)} / 512 KiB</small></div>
              {editorMode === "edit" ? <textarea id="knowledge-content" name="knowledge-content" className="knowledge-editor" value={draft.content} disabled={Boolean(busy) || Boolean(document?.archived)} spellCheck={false} onChange={(event) => setDraft((current) => ({ ...current, content: event.target.value }))} /> : <article className="knowledge-markdown-preview"><ReactMarkdown skipHtml>{draft.content}</ReactMarkdown></article>}
              <div className="knowledge-editor-actions">
                <button type="button" className="knowledge-primary-btn" disabled={Boolean(busy) || Boolean(document?.archived) || (!creating && !dirty)} onClick={() => void saveDocument()}>{busy === "save" ? "保存中..." : creating ? "创建文档" : "保存新版本"}</button>
                {document && !document.archived && <button type="button" className="knowledge-secondary-btn" disabled={Boolean(busy)} onClick={() => void setArchived(true)}>归档</button>}
                {document?.archived && <button type="button" className="knowledge-secondary-btn" disabled={Boolean(busy)} onClick={() => void setArchived(false)}>恢复文档</button>}
                {message && <span className={`knowledge-feedback is-${messageKind}`} role="status">{message}</span>}
              </div>
            </> : <div className="knowledge-editor-empty"><strong>选择或新建一份 Markdown 文档</strong><p>左侧文档会按文件路径稳定排序。</p></div>}
          </section>

          <aside className="knowledge-inspector-pane">
            <section className="knowledge-search-test"><div><span className="knowledge-eyebrow">RETRIEVAL TEST</span><h2>检索测试</h2></div><form onSubmit={(event) => { event.preventDefault(); void testSearch(); }}><input id="knowledge-search-query" name="knowledge-search-query" value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="输入代码或规则关键词" /><button type="submit" disabled={!searchQuery.trim() || busy === "search"}>{busy === "search" ? "检索中" : "检索"}</button></form><div className="knowledge-citations">{citations.map((item, index) => <article key={`${item.source}-${item.heading}-${index}`}><div><strong>{item.heading}</strong><b>{Math.min(100, Math.round(item.score * 100))}%</b></div><small>{item.source} · {item.version}</small><p>{item.excerpt}</p></article>)}{citations.length === 0 && <p className="knowledge-no-citation">输入关键词可验证当前已启用文档的召回结果。</p>}</div></section>
            {document && <section className="knowledge-history"><div><span className="knowledge-eyebrow">VERSION HISTORY</span><h2>版本记录</h2></div><dl><div><dt>当前版本</dt><dd>v{document.current_version}</dd></div><div><dt>内容指纹</dt><dd><code>{document.content_sha256.slice(0, 12)}</code></dd></div><div><dt>更新人</dt><dd>{document.updated_by}</dd></div><div><dt>更新时间</dt><dd>{formatDate(document.updated_at)}</dd></div></dl><div className="knowledge-version-list">{document.versions.map((version) => <div key={version.version}><span><strong>v{version.version}</strong><small>{formatDate(version.created_at)} · {version.created_by}</small></span>{version.version === document.current_version ? <b>当前</b> : <button type="button" disabled={Boolean(busy) || document.archived} onClick={() => void restoreVersion(version.version)}>恢复</button>}</div>)}</div></section>}
          </aside>
        </div>
      </main>
    </div>
  );
}
