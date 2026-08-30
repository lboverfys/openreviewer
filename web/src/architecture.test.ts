import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const SOURCE_ROOT = path.dirname(fileURLToPath(import.meta.url));

function lineCount(relativePath: string): number {
  const content = fs.readFileSync(path.resolve(SOURCE_ROOT, relativePath), "utf8");
  return content.split(/\r?\n/).length;
}

describe("前端模块边界", () => {
  it.each([
    "Auth.tsx",
    "DashboardPage.tsx",
    "SettingsPage.tsx",
    "AgentSettingsPanel.tsx",
    "ReviewDetailPage.tsx",
    "ReviewProgressPanels.tsx",
    "ReviewFindingCard.tsx",
  ])("%s 不重新膨胀为千行组件", (file) => {
    expect(lineCount(file)).toBeLessThanOrEqual(1_000);
  });

  it.each([
    "styles/base.css",
    "styles/auth.css",
    "styles/dashboard.css",
    "styles/settings.css",
    "styles/review.css",
    "styles/knowledge.css",
  ])("%s 保持在独立样式边界内", (file) => {
    expect(lineCount(file)).toBeLessThanOrEqual(2_000);
  });
});
