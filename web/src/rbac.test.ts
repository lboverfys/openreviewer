import { describe, expect, it } from "vitest";

import { allowedReviewActions, hasPermission, roleLabels } from "./rbac";
import type { AccessRole, AuthUser, Permission } from "./types";

function user(role: AccessRole, permissions: Permission[]): AuthUser {
  return {
    authenticated: true,
    username: role,
    role,
    permissions,
    expires_at: "2026-08-29T00:00:00Z",
  };
}

describe("role-aware frontend controls", () => {
  it("keeps viewers read-only", () => {
    const viewer = user("viewer", ["reviews:view"]);

    expect(hasPermission(viewer, "reviews:manage")).toBe(false);
    expect(allowedReviewActions(viewer, ["retry", "approve", "publish"])).toEqual([]);
  });

  it("separates adjudication, publishing and administration actions", () => {
    const adjudicator = user("adjudicator", [
      "reviews:view",
      "findings:adjudicate",
      "reviews:approve",
    ]);
    const publisher = user("publisher", ["reviews:view", "reviews:publish"]);
    const administrator = user("administrator", [
      "reviews:view",
      "findings:adjudicate",
      "reviews:approve",
      "reviews:publish",
      "reviews:manage",
      "settings:manage",
      "knowledge:manage",
    ]);
    const actions = ["retry", "approve", "reject", "publish"] as const;

    expect(allowedReviewActions(adjudicator, actions)).toEqual(["approve", "reject"]);
    expect(allowedReviewActions(publisher, actions)).toEqual(["publish"]);
    expect(allowedReviewActions(administrator, actions)).toEqual(actions);
    expect(roleLabels.administrator).toBe("管理员");
  });
});
