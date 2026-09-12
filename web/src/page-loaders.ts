import { api } from "./api";
import { platformApi } from "./platform-api";

export const pageLoaders = {
  dashboard: () => import("./DashboardPage"),
  knowledge: () => import("./KnowledgePage"),
  review: () => import("./ReviewDetailPage"),
  settings: () => import("./SettingsPage"),
  retrieval: () => import("./RetrievalPage"),
  team: () => import("./TeamPage"),
  evaluations: () => import("./EvaluationPage"),
  platform: () => import("./PlatformPage"),
};

export function preloadPage(page: keyof typeof pageLoaders) {
  // 页面代码与首屏GET同时开始，避免点开后先等JS、再等接口的串行等待。
  // 复用有界会话缓存和请求合并；导航失败仍交给页面自己的错误入口。
  void pageLoaders[page]().catch(() => undefined);
  const reads = {
    dashboard: () => api.dashboard(),
    settings: () => api.aiSettings(),
    knowledge: () => api.knowledgeDocuments(),
    retrieval: () => api.retrievalIndexes(),
    team: () => api.teamRepositories(),
    evaluations: () => api.evaluationDatasets(),
    platform: () => platformApi.workItems(true, "", false),
  };
  if (page !== "review") void reads[page]().catch(() => undefined);
}
