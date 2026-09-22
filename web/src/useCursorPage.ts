import { useCallback, useEffect, useRef, useState } from "react";

import { isRequestAborted, peekReadCache, subscribeReadCache } from "./api";

export interface CursorPage<T> {
  items: T[];
  next_cursor?: string | null;
  total?: number;
}

export function pageCacheKey(key: string, cursor?: string) {
  return `${key}:${cursor ?? "first"}`;
}

export function useCursorPage<T>({ cacheKey, load, onError, enabled = true }: {
  cacheKey: string;
  load: (cursor?: string, signal?: AbortSignal, force?: boolean) => Promise<CursorPage<T>>;
  onError: (error: unknown) => void;
  enabled?: boolean;
}) {
  const [position, setPosition] = useState<{ key: string; cursors: (string | undefined)[] }>({ key: cacheKey, cursors: [undefined] });
  const cursors = position.key === cacheKey ? position.cursors : [undefined];
  const cursor = cursors.at(-1);
  const key = pageCacheKey(cacheKey, cursor);
  const [result, setResult] = useState<{ key: string; data: CursorPage<T> } | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const requestSequence = useRef(0);
  const data = result?.key === key ? result.data : peekReadCache<CursorPage<T>>(key);

  const refresh = useCallback(async (force = true, signal?: AbortSignal) => {
    const sequence = ++requestSequence.current;
    setPending(key);
    try {
      const next = await load(cursor, signal, force);
      if (!signal?.aborted && sequence === requestSequence.current) setResult({ key, data: next });
    } catch (error) {
      if (!signal?.aborted && sequence === requestSequence.current && !isRequestAborted(error)) onError(error);
    } finally {
      if (sequence === requestSequence.current) setPending((current) => current === key ? null : current);
    }
  }, [cursor, key, load, onError]);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    const unsubscribe = subscribeReadCache<CursorPage<T>>(key, (next) => {
      if (!controller.signal.aborted) setResult({ key, data: next });
    });
    void refresh(false, controller.signal);
    return () => { requestSequence.current += 1; controller.abort(); unsubscribe(); };
  }, [enabled, key, refresh]);

  return {
    data, cursor, page: cursors.length,
    loading: enabled && pending === key,
    refresh,
    reset: () => setPosition({ key: cacheKey, cursors: [undefined] }),
    previous: () => setPosition({ key: cacheKey, cursors: cursors.slice(0, -1) }),
    next: () => {
      if (data?.next_cursor) setPosition({ key: cacheKey, cursors: [...cursors, data.next_cursor] });
    },
  };
}
