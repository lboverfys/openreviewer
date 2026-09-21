import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";
import { Textarea } from "./components/ui/textarea";
import { DetailDialog, Notice } from "./Feedback";
import { ChangeEvent, lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";

import { api, ApiError, peekReadCache, subscribeReadCache } from "./api";
import Pagination, { PAGE_SIZE } from "./Pagination";
import type { KnowledgeCitation, KnowledgeDocument, KnowledgeLibrary, KnowledgeMutation, KnowledgeVersion } from "./types";
import { useCursorPage } from "./useCursorPage";
import { errorMessage, formatDate } from "./utils";

const ReactMarkdown = lazy(() => import("react-markdown"));

interface KnowledgePageProps {
  onSignedOut: (message?: string) => void;
}

interface DocumentDraft {
  source: string;
  content: string;
  enabled: boolean;
  repository_scope: string;
}

const EMPTY_DRAFT: DocumentDraft = {
  source: "new-rule.md",
  content: "# 新规则\n\n在这里填写审查规则。\n",
  enabled: true,
  repository_scope: "lboverfys/NiuMa",
};

function documentDraft(document: KnowledgeDocument): DocumentDraft {
  return { source: document.source, content: document.content, enabled: document.enabled,
    repository_scope: document.repository_scope ?? "" };
}

function draftChanged(document: KnowledgeDocument | null, draft: DocumentDraft): boolean {
  const original = document ? documentDraft(document) : EMPTY_DRAFT;
  return draft.source !== original.source || draft.content !== original.content
    || draft.enabled !== original.enabled || draft.repository_scope !== original.repository_scope;
}

function libraryKey(archived: boolean, offset: number, query: string): string {
  return "knowledge-documents:" + (archived ? "archived-only" : "active") + ":" + offset + ":" + query;
}

export default function KnowledgePage({ onSignedOut }: KnowledgePageProps) {
  const [archivedView, setArchivedView] = useState(false);
  const [offset, setOffset] = useState(0);
  const [listQuery, setListQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const cachedLibrary = peekReadCache<KnowledgeLibrary>(libraryKey(archivedView, offset, debouncedQuery));
  const firstId = cachedLibrary?.items[0]?.id;
  const cachedDocument = firstId ? peekReadCache<KnowledgeDocument>("knowledge-document:" + firstId) : undefined;
  const [library, setLibrary] = useState<KnowledgeLibrary | null>(cachedLibrary ?? null);
  const [document, setDocument] = useState<KnowledgeDocument | null>(cachedDocument ?? null);
  const [draft, setDraft] = useState<DocumentDraft>(cachedDocument ? documentDraft(cachedDocument) : EMPTY_DRAFT);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [messageKind, setMessageKind] = useState<"success" | "error">("success");
  const [lastRemoved, setLastRemoved] = useState<KnowledgeDocument | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [citations, setCitations] = useState<KnowledgeCitation[]>([]);
  const uploadRef = useRef<HTMLInputElement>(null);
  const documentRef = useRef(document);
  const draftRef = useRef(draft);
  const creatingRef = useRef(creating);
  const openRequest = useRef(0);
  documentRef.current = document;
  draftRef.current = draft;
  creatingRef.current = creating;
  const dirty = draftChanged(document, draft);
  const visibleItems = library?.items ?? [];

  const handleError = useCallback((reason: unknown) => {
    if (reason instanceof ApiError && reason.status === 401) {
      onSignedOut("登录状态已失效，请重新登录");
      return;
    }
    setMessageKind("error");
    setMessage(errorMessage(reason));
  }, [onSignedOut]);

  const selectDocument = useCallback((next: KnowledgeDocument | null) => {
    const nextDraft = next ? documentDraft(next) : EMPTY_DRAFT;
    documentRef.current = next;
    draftRef.current = nextDraft;
    creatingRef.current = false;
    setDocument(next);
    setDraft(nextDraft);
    setCreating(false);
    setEditing(false);
    setHistoryOpen(false);
  }, []);

  const openDocument = useCallback(async (id: string, signal?: AbortSignal) => {
    const request = ++openRequest.current;
    setBusy("open");
    try {
      const next = await api.knowledgeDocument(id, signal);
      if (signal?.aborted || request !== openRequest.current) return;
      selectDocument(next);
    } catch (reason) {
      if (!signal?.aborted && request === openRequest.current) handleError(reason);
    } finally {
      if (!signal?.aborted && request === openRequest.current) setBusy("");
    }
  }, [handleError, selectDocument]);

  const refreshLibrary = useCallback(async (signal?: AbortSignal, openTarget = true) => {
    const selection = openRequest.current;
    setBusy(current => current || "refresh");
    try {
      const next = await api.knowledgeDocuments(archivedView, signal, false, offset, debouncedQuery, archivedView);
      if (signal?.aborted) return;
      setLibrary(next);
      if (next.items.length === 0 && offset > 0 && next.total > 0) {
        setOffset(Math.max(0, offset - PAGE_SIZE));
        return;
      }
      if (!openTarget || creatingRef.current || selection !== openRequest.current
          || draftChanged(documentRef.current, draftRef.current)) return;
      const target = next.items.find(item => item.id === documentRef.current?.id) ?? next.items[0];
      if (target) await openDocument(target.id, signal);
      else selectDocument(null);
    } catch (reason) {
      if (!signal?.aborted) handleError(reason);
    } finally {
      if (!signal?.aborted) setBusy("");
    }
  }, [archivedView, offset, debouncedQuery, openDocument, selectDocument, handleError]);

  useEffect(() => subscribeReadCache<KnowledgeLibrary>(
    libraryKey(archivedView, offset, debouncedQuery), setLibrary,
  ), [archivedView, offset, debouncedQuery]);

  useEffect(() => {
    const id = document?.id;
    if (!id) return undefined;
    return subscribeReadCache<KnowledgeDocument>("knowledge-document:" + id, next => {
      const current = documentRef.current;
      if (!current || current.id !== next.id || draftChanged(current, draftRef.current)) return;
      documentRef.current = next;
      draftRef.current = documentDraft(next);
      setDocument(next);
      setDraft(documentDraft(next));
    });
  }, [document?.id]);

  useEffect(() => {
    const controller = new AbortController();
    void refreshLibrary(controller.signal, !documentRef.current);
    return () => controller.abort();
  }, [refreshLibrary]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedQuery(listQuery.trim());
      setOffset(0);
    }, 300);
    return () => window.clearTimeout(timer);
  }, [listQuery]);

  useEffect(() => {
    function warnBeforeUnload(event: BeforeUnloadEvent) {
      if (dirty) event.preventDefault();
    }
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [dirty]);

  const loadHistory = useCallback(async (cursor?: string, signal?: AbortSignal, force = false) => {
    const next = await api.knowledgeDocument(document!.id, signal, force, cursor);
    return { items: next.versions, next_cursor: next.version_next_cursor };
  }, [document?.id]);
  const history = useCursorPage<KnowledgeVersion>({
    cacheKey: "knowledge-history:" + (document?.id ?? "none") + ":" + (document?.current_version ?? 0),
    load: loadHistory, onError: handleError, enabled: Boolean(document) && historyOpen,
  });

  function allowLeavingDraft() {
    return !dirty || window.confirm("当前修改还没保存。放弃修改并继续吗？");
  }

  function changeView(archived: boolean) {
    if (archived === archivedView || !allowLeavingDraft()) return;
    ++openRequest.current;
    selectDocument(null);
    setLibrary(null);
    setOffset(0);
    setListQuery("");
    setDebouncedQuery("");
    setArchivedView(archived);
  }

  function applyMutation(result: KnowledgeMutation, feedback: string, removed: KnowledgeDocument | null = null, refreshList = true) {
    selectDocument(result.document);
    setLibrary(current => current ? { ...current, revision: result.revision } : current);
    setMessageKind("success");
    setMessage(feedback);
    setLastRemoved(removed);
    // 变更结果已经带正文，刷新列表时不重复请求详情。
    if (refreshList) void refreshLibrary(undefined, false);
  }

  async function saveDocument() {
    if (!library) return;
    setBusy("save");
    setMessage("");
    try {
      const payload = { expected_revision: library.revision, source: draft.source,
        content: draft.content, enabled: draft.enabled, repository_scope: draft.repository_scope.trim() || null };
      const result = creating
        ? await api.createKnowledgeDocument(payload)
        : await api.updateKnowledgeDocument(document!.id, { ...payload, expected_document_version: document!.current_version });
      applyMutation(result, creating ? "文档已保存并开启使用。" : "修改已保存。");
    } catch (reason) {
      handleError(reason);
      // 冲突时保留草稿，避免刷新详情吞掉尚未保存的正文。
      if (reason instanceof ApiError && reason.status === 409) void refreshLibrary(undefined, false);
    } finally {
      setBusy("");
    }
  }

  async function toggleEnabled() {
    if (!library || !document || dirty) return;
    setBusy("enable");
    setMessage("");
    try {
      const result = await api.updateKnowledgeDocument(document.id, {
        expected_revision: library.revision, expected_document_version: document.current_version,
        source: document.source, content: document.content, enabled: !document.enabled,
        repository_scope: document.repository_scope ?? null,
      });
      applyMutation(result, result.document.enabled
        ? "已开启使用，AI 可以按相关性检索这份规则。"
        : "已暂停使用，文档仍留在列表中。");
    } catch (reason) { handleError(reason); }
    finally { setBusy(""); }
  }

  async function removeDocument() {
    if (!library || !document || dirty) return;
    setBusy("archive");
    setMessage("");
    try {
      const previous = document;
      const result = await api.setKnowledgeDocumentArchived(document.id, true, library.revision, document.current_version);
      applyMutation(result, "《" + previous.title + "》已删除到回收站，可撤销或在“回收站”中找回。", previous);
    } catch (reason) { handleError(reason); }
    finally { setBusy(""); }
  }

  async function restoreDocument(target: KnowledgeDocument, enabled = true) {
    if (!library || dirty) return;
    setBusy("restore");
    setMessage("");
    try {
      const result = await api.setKnowledgeDocumentArchived(target.id, false, library.revision, target.current_version, enabled);
      applyMutation(result, enabled ? "文档已恢复并开启使用。" : "文档已恢复到列表，保持暂停使用。", null, !archivedView);
      if (archivedView) {
        setArchivedView(false);
        setOffset(0);
        setListQuery("");
        setDebouncedQuery("");
      }
    } catch (reason) { handleError(reason); }
    finally { setBusy(""); }
  }

  async function restoreVersion(version: number) {
    if (!library || !document || version === document.current_version) return;
    if (!window.confirm("把正文改回第 " + version + " 版吗？当前正文仍会保存在历史版本中。")) return;
    setBusy("version");
    try {
      const result = await api.restoreKnowledgeVersion(document.id, version, library.revision, document.current_version);
      applyMutation(result, "已采用第 " + version + " 版的内容，原正文仍保留在历史中。");
    } catch (reason) { handleError(reason); }
    finally { setBusy(""); }
  }

  async function testSearch() {
    if (!searchQuery.trim()) return;
    setBusy("search");
    try {
      setCitations((await api.searchKnowledge(searchQuery.trim(), 8, draft.repository_scope.trim() || undefined)).items);
    } catch (reason) { handleError(reason); }
    finally { setBusy(""); }
  }

  function startNewDocument() {
    if (!allowLeavingDraft()) return;
    ++openRequest.current;
    selectDocument(null);
    creatingRef.current = true;
    setCreating(true);
    setEditing(true);
    setMessage("");
    setLastRemoved(null);
  }

  async function uploadMarkdown(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file || !allowLeavingDraft()) return;
    if (!file.name.toLocaleLowerCase().endsWith(".md") || file.size > 512 * 1024) {
      setMessageKind("error");
      setMessage("请选择不超过 512 KiB 的 .md 文件。");
      return;
    }
    try {
      const content = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
      ++openRequest.current;
      selectDocument(null);
      creatingRef.current = true;
      const next = { ...EMPTY_DRAFT, source: file.name, content };
      draftRef.current = next;
      setDraft(next);
      setCreating(true);
      setEditing(true);
      setMessage("");
      setLastRemoved(null);
    } catch {
      setMessageKind("error");
      setMessage("文件编码无法识别，请另存为 UTF-8 后导入。");
    }
  }

  async function deleteDocument() {
    if (!library || !document || !window.confirm("彻底删除这份文档及全部正文版本？无法恢复；旧审查报告已保存的引用会保留。")) return;
    setBusy("delete");
    try {
      await api.deleteKnowledgeDocument(document.id, library.revision, document.current_version);
      selectDocument(null); setLastRemoved(null); setEditing(false);
      setMessageKind("success"); setMessage("文档已彻底删除，不再用于新审查；旧报告仍可查看。");
      await refreshLibrary(undefined, false);
    } catch (reason) { handleError(reason); } finally { setBusy(""); }
  }

  return (
    <div className="knowledge-shell">
      <main className="knowledge-main">
        <header className="knowledge-hero">
          <div>
            <h1>规则文档</h1>
            <p>保存 AI 审查时可以参考的规则。按仓库和相关性选取，不会每次把全部文档都发给 AI。</p>
          </div>
          <nav className="related-actions"><a href="#retrieval">搜索代码</a></nav>
        </header>
        {message && <Notice kind={messageKind} onDismiss={() => setMessage("")}>{message}{lastRemoved && messageKind === "success" && <Button variant="outline" type="button" disabled={Boolean(busy) || dirty} onClick={() => void restoreDocument(lastRemoved, lastRemoved.enabled)}>撤销删除</Button>}</Notice>}
        <div className="knowledge-workspace">
          <aside className="knowledge-document-pane" aria-label="知识文档列表">
            <div className="knowledge-pane-heading"><strong>规则文档</strong><span>{library?.enabled_count ?? 0} 份使用中</span></div>
            <div className="knowledge-list-tools">
              <div className="knowledge-view-tabs" role="tablist" aria-label="文档范围">
                <Button variant="outline" type="button" role="tab" aria-selected={!archivedView} disabled={Boolean(busy)}
                  onClick={() => changeView(false)}>文档列表</Button>
                <Button variant="outline" type="button" role="tab" aria-selected={archivedView} disabled={Boolean(busy)}
                  onClick={() => changeView(true)}>回收站</Button>
              </div>
              <Input aria-label="搜索文档" value={listQuery} onChange={event => setListQuery(event.target.value)} placeholder="搜索名称或正文" />
              {archivedView && <p>删除的文档保留在这里，可以恢复或彻底删除。</p>}
            </div>
            <div className="knowledge-document-list">
              {visibleItems.map(item => <Button variant="outline" key={item.id} type="button"
                className={document?.id === item.id ? "is-selected" : ""}
                disabled={Boolean(busy) && busy !== "open"}
                onClick={() => { if (allowLeavingDraft()) void openDocument(item.id); }}>
                <span><strong>{item.title}</strong><small>{item.source}</small></span>
                <i>{item.archived ? "已移出" : item.enabled ? "使用中" : "已暂停"}</i>
              </Button>)}
              {!busy && visibleItems.length === 0 && <p className="knowledge-list-empty">
                {archivedView ? "没有已移出的文档。" : "没有符合条件的文档。"}
              </p>}
            </div>
            <Pagination page={Math.floor(offset / PAGE_SIZE) + 1} count={visibleItems.length} total={library?.total}
              busy={Boolean(busy)} hasNext={Boolean(library?.has_more)}
              onPrevious={() => setOffset(value => Math.max(0, value - PAGE_SIZE))}
              onNext={() => setOffset(value => value + PAGE_SIZE)} label="知识文档分页" />
            <div className="knowledge-list-actions">
              <Button variant="outline" type="button" disabled={Boolean(busy)} onClick={startNewDocument}>新建文档</Button>
              <Button variant="outline" type="button" disabled={Boolean(busy)} onClick={() => uploadRef.current?.click()}>导入 .md 文件</Button>
              <Input ref={uploadRef} type="file" accept=".md,text/markdown,text/plain" hidden onChange={event => void uploadMarkdown(event)} />
            </div>

          </aside>

          <section className="knowledge-editor-pane" aria-label="文档内容">
            {creating || document ? <>
              <div className="knowledge-document-heading">
                <div><h2>{creating ? "新建规则文档" : document?.title}</h2>
                  <small>{creating ? "写好后保存即可使用。" : document?.source + " · 正文第 " + document?.current_version + " 版"}</small></div>
                {document && !document.archived && !editing && <Button variant="outline" type="button" onClick={() => setEditing(true)} disabled={Boolean(busy)}>编辑文档</Button>}
              </div>
              {document && <section className={"knowledge-use-status" + (document.archived ? " is-removed" : "")} aria-label="文档使用状态">
                <div><strong>{document.archived ? "已删除到回收站" : document.enabled ? "AI 可以使用这份规则" : "已暂停使用"}</strong>
                  <p>{document.archived ? "正文和历史版本都在，恢复后即可重新使用。"
                    : document.enabled ? "暂停后仍留在列表，点击按钮直接保存，无需再点保存。"
                    : "文档仍保留。开启后，AI 才能从当前知识库检索这份规则。"}</p></div>
                {document.archived ? <Button variant="default" type="button" className="knowledge-primary-btn" disabled={Boolean(busy)}
                  onClick={() => void restoreDocument(document)}>恢复并使用</Button>
                  : <Button variant="outline" type="button" disabled={Boolean(busy) || dirty} onClick={() => void toggleEnabled()}>
                    {busy === "enable" ? "正在保存…" : document.enabled ? "暂停使用" : "开启使用"}</Button>}
              </section>}
              {document && <Button variant="outline" type="button" disabled={Boolean(busy) || dirty} onClick={() => void deleteDocument()}>彻底删除文档</Button>}
              {editing ? <>
                <div className="knowledge-document-fields">
                  <label>文件名<Input value={draft.source} maxLength={200} disabled={Boolean(busy)}
                    onChange={event => setDraft(current => ({ ...current, source: event.target.value }))} /></label>
                  <label>适用仓库<Input value={draft.repository_scope} maxLength={255} placeholder="留空表示通用规则" disabled={Boolean(busy)}
                    onChange={event => setDraft(current => ({ ...current, repository_scope: event.target.value }))} /></label>
                  <small>文件名以 .md 结尾；仓库填写 owner/repository，留空表示所有仓库通用。</small>
                </div>
                <label className="knowledge-body-label">规则正文
                  <Textarea className="knowledge-editor" value={draft.content} spellCheck={false} disabled={Boolean(busy)}
                    onChange={event => setDraft(current => ({ ...current, content: event.target.value }))} />
                </label>
                <div className="knowledge-editor-actions">
                  <Button variant="default" type="button" className="knowledge-primary-btn" disabled={Boolean(busy) || (!creating && !dirty)}
                    onClick={() => void saveDocument()}>{busy === "save" ? "正在保存…" : creating ? "保存并使用" : "保存修改"}</Button>
                  <Button variant="outline" type="button" disabled={Boolean(busy)} onClick={() => selectDocument(document)}>取消编辑</Button>
                  {dirty && <span>有未保存的修改</span>}
                </div>
              </> : <article className="knowledge-markdown-preview">
                <Suspense fallback={<p role="status">正在显示正文…</p>}><ReactMarkdown skipHtml>{draft.content}</ReactMarkdown></Suspense>
              </article>}
              {document && !document.archived && <div className="knowledge-remove-action">
                <p>暂时不用可“暂停使用”；不再常用可删除到回收站，之后仍能恢复。</p>
                <Button variant="outline" type="button" disabled={Boolean(busy) || dirty} onClick={() => void removeDocument()}>删除到回收站</Button>
              </div>}
              <p className="knowledge-snapshot-note">这些操作影响当前知识库；已有审查保留原来的引用；删除后固定方案也不会在新任务中使用这份文档。</p>
              <div className="knowledge-extra-tools">
                {document && <DetailDialog open={historyOpen} onToggle={event => setHistoryOpen(event.currentTarget.open)}>
                  <summary>历史版本</summary>
                  {historyOpen && <div className="knowledge-history">
                    <p>查看以前的正文，或采用某一版内容。恢复正文不会删除其他版本。</p>
                    <div className="knowledge-version-list">{(history.data?.items ?? document.versions).map(version =>
                      <div key={version.version}><span><strong>第 {version.version} 版</strong>
                        <small>{formatDate(version.created_at)} · {version.created_by}</small></span>
                        {version.version === document.current_version ? <b>当前正文</b>
                          : <Button variant="outline" type="button" disabled={Boolean(busy) || document.archived || dirty}
                            onClick={() => void restoreVersion(version.version)}>采用这版内容</Button>}
                      </div>)}</div>
                    <Pagination page={history.page} count={(history.data?.items ?? document.versions).length}
                      hasNext={Boolean(history.data ? history.data.next_cursor : document.version_next_cursor)}
                      busy={history.loading} onPrevious={history.previous} onNext={history.next} label="文档版本分页" />
                  </div>}
                </DetailDialog>}
                <DetailDialog open={searchOpen} onToggle={event => setSearchOpen(event.currentTarget.open)}>
                  <summary>试搜可用规则</summary>
                  {searchOpen && <section className="knowledge-search-test">
                    <p>只查询已经保存并启用的规则，不发起模型审查。</p>
                    <form onSubmit={event => { event.preventDefault(); void testSearch(); }}>
                      <Input aria-label="规则关键词" value={searchQuery} onChange={event => setSearchQuery(event.target.value)} placeholder="例如：退款、权限、标签删除" />
                      <Button variant="outline" type="submit" disabled={!searchQuery.trim() || Boolean(busy)}>{busy === "search" ? "查找中…" : "查找规则"}</Button>
                    </form>
                    <div className="knowledge-citations">{citations.map((item, index) =>
                      <article key={item.source + ":" + item.heading + ":" + index}><strong>{item.heading}</strong><small>{item.source}</small><p>{item.excerpt}</p></article>
                    )}</div>
                  </section>}
                </DetailDialog>
              </div>
            </> : <div className="knowledge-editor-empty"><h2>{archivedView ? "找回已移出的文档" : "选择一份规则文档"}</h2>
              <p>{archivedView ? "从左侧选择文档，点击“恢复并使用”。" : "从左侧选择文档阅读，也可以新建或导入自己的规则。"}</p>
            </div>}
          </section>
        </div>
      </main>
    </div>
  );
}
