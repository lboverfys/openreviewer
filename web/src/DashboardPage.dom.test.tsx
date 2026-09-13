// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import { Login } from "./Auth";

vi.mock("./credentials", () => ({
  loadSavedCredentials: vi.fn().mockResolvedValue(null),
  saveCredentials: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      login: vi.fn(),
    },
  };
});

describe("登录组件", () => {
  beforeEach(() => {
    vi.mocked(api.login).mockReset();
  });

  it("提交明确的 submit 按钮并把规范化凭据交给 API", async () => {
    const user = userEvent.setup();
    const onAuthenticated = vi.fn();
    vi.mocked(api.login).mockResolvedValue({
      authenticated: true,
      username: "reviewer",
      role: "administrator",
      permissions: ["reviews:view", "reviews:manage"],
      expires_at: "2026-08-30T00:00:00Z",
    });
    render(<Login onAuthenticated={onAuthenticated} />);

    await user.type(screen.getByLabelText("账号"), "  reviewer  ");
    await user.type(screen.getByLabelText("密码"), "correct-password");
    const submit = screen.getByRole("button", { name: /登录平台/ });
    expect(submit).toHaveAttribute("type", "submit");
    await user.click(submit);

    expect(api.login).toHaveBeenCalledWith("reviewer", "correct-password");
    expect(onAuthenticated).toHaveBeenCalledWith(
      expect.objectContaining({ username: "reviewer" }),
    );
  });
});
