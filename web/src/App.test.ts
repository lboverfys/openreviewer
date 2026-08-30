import { describe, expect, it } from "vitest";

import { readAppView } from "./App";

describe("应用 Hash 路由", () => {
  it("解析固定管理页面", () => {
    expect(readAppView("#settings")).toEqual({ kind: "settings" });
    expect(readAppView("#knowledge")).toEqual({ kind: "knowledge" });
  });

  it("解码审查任务标识", () => {
    expect(readAppView("#review/run%2F2026-08-29")).toEqual({
      kind: "review",
      reviewRunId: "run/2026-08-29",
    });
  });

  it("损坏或空的审查地址安全回到仪表盘", () => {
    expect(readAppView("#review/%E0%A4%A")).toEqual({ kind: "dashboard" });
    expect(readAppView("#review/")).toEqual({ kind: "dashboard" });
  });
});
