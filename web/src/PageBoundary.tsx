import { Button } from "./components/ui/button";
import { Component, Suspense, type ReactNode } from "react";

import { Brand, LoadingScreen } from "./Auth";

// 发布后旧页面的资源可能已失效；由用户刷新获取新入口，避免自动重试循环。
export default class PageBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (this.state.failed) {
      return <main className="loading-screen">
        <div className="loading-card" role="alert">
          <Brand />
          <p className="loading-hint">页面加载失败，请检查网络后刷新重试。</p>
          <Button variant="outline" type="button" onClick={() => window.location.reload()}>刷新页面</Button>
        </div>
      </main>;
    }
    return <Suspense fallback={<LoadingScreen />}>{this.props.children}</Suspense>;
  }
}
