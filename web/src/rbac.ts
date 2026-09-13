import type {
  AccessRole,
  AuthUser,
  Permission,
  ReviewAction,
} from "./types";

export const roleLabels: Record<AccessRole, string> = {
  viewer: "只读成员",
  adjudicator: "问题核对员",
  publisher: "结果发布员",
  administrator: "管理员",
};

const actionPermissions: Record<ReviewAction, Permission> = {
  start: "reviews:manage",
  pause: "reviews:manage",
  resume: "reviews:manage",
  retry_stage: "reviews:manage",
  approve: "reviews:approve",
  reject: "reviews:approve",
  publish: "reviews:publish",
  expedite: "reviews:manage",
  retry: "reviews:manage",
  cancel: "reviews:manage",
  rerun: "reviews:manage",
  retry_failed_node: "reviews:manage",
  new_review: "reviews:manage",
  review_snapshot: "reviews:manage",
};

export function hasPermission(user: AuthUser, permission: Permission): boolean {
  return user.permissions.includes(permission);
}

export function allowedReviewActions(
  user: AuthUser,
  actions: readonly ReviewAction[],
): ReviewAction[] {
  return actions.filter((action) => hasPermission(user, actionPermissions[action]));
}
