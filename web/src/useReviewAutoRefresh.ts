import { useEffect, useRef } from "react";

import { api, ApiError } from "./api";

// 详情页只需要在任务运行期间保持近实时；变更令牌接口虽然轻量，
// 过短的间隔仍会在多个管理员标签页下放大请求量。
export const REVIEW_CHANGE_POLL_MS = 5_000;

interface ReviewAutoRefreshOptions {
  enabled: boolean;
  reviewRunId: string;
  changeToken: string | null;
  onChanged: (signal: AbortSignal) => Promise<string | null>;
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
    let polling = false;
    let pollAgain = false;

    const schedule = (delay = REVIEW_CHANGE_POLL_MS) => {
      if (!active || document.visibilityState === "hidden") return;
      // 可见性切换或组件重渲染可能在一次请求尚未结束时再次触发
      // schedule；只排队一次，避免同时打出多个 change-token 请求。
      if (polling) {
        pollAgain = true;
        return;
      }
      if (timer !== undefined) window.clearTimeout(timer);
      timer = window.setTimeout(() => void poll(), delay);
    };

    const poll = async () => {
      if (!active || document.visibilityState === "hidden" || polling) return;
      polling = true;
      timer = undefined;
      try {
        const next = await api.reviewChangeToken(
          reviewRunId,
          controller.signal,
        );
        // AbortController 取消后，某些 fetch mock/代理仍可能把已经排队的
        // 响应交回来；此时组件可能已卸载，不能再触发详情请求或状态更新。
        if (!active || controller.signal.aborted) return;
        if (next.change_token !== knownToken) {
          const refreshedToken = await onChangedRef.current(controller.signal);
          // 变更令牌查询和详情查询不是同一个数据库快照。详情若仍返回旧
          // 令牌，不能提前把目标令牌标记为已处理；保留 knownToken 后，
          // 下一轮会继续追赶，直到详情实际包含这次变化。
          if (
            active
            && !controller.signal.aborted
            && refreshedToken === next.change_token
          ) {
            knownToken = next.change_token;
          }
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
        polling = false;
        if (pollAgain) {
          pollAgain = false;
          schedule(0);
        } else {
          schedule();
        }
      }
    };

    schedule();
    const onVisibilityChange = () => {
      if (!active) return;
      if (document.visibilityState === "hidden") {
        if (timer !== undefined) {
          window.clearTimeout(timer);
          timer = undefined;
        }
        // 当前请求仍可完成；回到前台后只需要一次立即校验。
        pollAgain = false;
        return;
      }
      // 回到前台时立即校验一次，避免用户看到过期状态数秒。
      schedule(0);
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      active = false;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [changeToken, enabled, reviewRunId]);
}
