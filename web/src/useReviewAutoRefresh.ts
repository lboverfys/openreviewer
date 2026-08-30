import { useEffect, useRef } from "react";

import { api, ApiError } from "./api";

export const REVIEW_CHANGE_POLL_MS = 2_500;

interface ReviewAutoRefreshOptions {
  enabled: boolean;
  reviewRunId: string;
  changeToken: string | null;
  onChanged: (signal: AbortSignal) => Promise<boolean>;
  onSignedOut: (message?: string) => void;
  onError: (reason: unknown) => void;
}

export function useReviewAutoRefresh({
  enabled,
  reviewRunId,
  changeToken,
  onChanged,
  onSignedOut,
  onError,
}: ReviewAutoRefreshOptions) {
  const onChangedRef = useRef(onChanged);
  const onSignedOutRef = useRef(onSignedOut);
  const onErrorRef = useRef(onError);
  onChangedRef.current = onChanged;
  onSignedOutRef.current = onSignedOut;
  onErrorRef.current = onError;

  useEffect(() => {
    if (!enabled || changeToken === null) return undefined;

    const controller = new AbortController();
    let active = true;
    let knownToken = changeToken;
    let timer: number | undefined;

    const schedule = () => {
      if (!active) return;
      timer = window.setTimeout(() => void poll(), REVIEW_CHANGE_POLL_MS);
    };

    const poll = async () => {
      try {
        const next = await api.reviewChangeToken(
          reviewRunId,
          controller.signal,
        );
        if (next.change_token !== knownToken) {
          const refreshed = await onChangedRef.current(controller.signal);
          if (refreshed) knownToken = next.change_token;
        }
      } catch (reason) {
        if (controller.signal.aborted) return;
        if (reason instanceof ApiError && reason.status === 401) {
          active = false;
          onSignedOutRef.current("登录状态已失效，请重新登录");
          return;
        }
        onErrorRef.current(reason);
      } finally {
        schedule();
      }
    };

    schedule();
    return () => {
      active = false;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [changeToken, enabled, reviewRunId]);
}
