import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import "./styles.css";

/**
 * 浏览器入口：把 React 根组件挂载到 index.html 的 `#root` 节点。
 *
 * 这里只负责一次性初始化 React 和全局样式，不读取会话、不创建 API 客户端，也
 * 不决定显示哪个页面；`App` 会在挂载后自行检查 Cookie 并管理会话状态。StrictMode
 * 只影响开发期检查，不改变生产构建的业务流程。
 */
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
