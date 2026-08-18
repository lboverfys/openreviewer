import { describe, expect, it } from "vitest";

import { formatDate, shortSha, statusLabels } from "./utils";

describe("dashboard formatting", () => {
  // 每个用例只验证一个展示契约，避免把日期、SHA 和状态标签失败混在一起。
  it("shortens a full commit SHA without changing its prefix", () => {
    // Given：一个长度大于八位的提交 SHA；When：交给表格短显示函数；Then：只截取前缀。
    expect(shortSha("abcdef0123456789")).toBe("abcdef01");
  });

  it("uses a safe placeholder for invalid dates", () => {
    // 无效文本和明确的 null 都不能把 JavaScript 的 Invalid Date 直接显示给用户。
    expect(formatDate("not-a-date")).toBe("—");
    expect(formatDate(null)).toBe("—");
  });

  it("keeps an explicit label for every visible task state", () => {
    // 关键状态必须有中文标签，否则状态卡片会出现未翻译的机器枚举值。
    expect(statusLabels.waiting_for_ci).toBe("等待 CI");
    expect(statusLabels.failed).toBe("失败");
  });
});
