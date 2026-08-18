import { describe, expect, it } from "vitest";

import { formatDate, shortSha, statusLabels } from "./utils";

describe("dashboard formatting", () => {
  it("shortens a full commit SHA without changing its prefix", () => {
    expect(shortSha("abcdef0123456789")).toBe("abcdef01");
  });

  it("uses a safe placeholder for invalid dates", () => {
    expect(formatDate("not-a-date")).toBe("—");
    expect(formatDate(null)).toBe("—");
  });

  it("keeps an explicit label for every visible task state", () => {
    expect(statusLabels.waiting_for_ci).toBe("等待 CI");
    expect(statusLabels.failed).toBe("失败");
  });
});
