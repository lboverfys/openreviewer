export const PAGE_SIZE = 10;

interface PaginationProps {
  page: number;
  hasNext: boolean;
  busy?: boolean;
  count: number;
  total?: number;
  label?: string;
  onPrevious: () => void;
  onNext: () => void;
}

export default function Pagination({ page, hasNext, busy = false, count, total, label = "列表分页", onPrevious, onNext }: PaginationProps) {
  return <nav className="list-pagination" aria-label={label}>
    <span>第 {page} 页 · 本页 {count} 条{total !== undefined ? ` · 共 ${total} 条` : ""}</span>
    <div>
      <button type="button" disabled={busy || page <= 1} onClick={onPrevious}>上一页</button>
      <button type="button" disabled={busy || !hasNext} onClick={onNext}>下一页</button>
    </div>
    {busy && <span role="status">正在加载…</span>}
  </nav>;
}
