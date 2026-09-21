// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import SnapshotReviewDialog from "./SnapshotReviewDialog";

afterEach(cleanup);

it("复查配置在独立弹窗中，取消和 Escape 不触发模型动作", async () => {
  const confirm = vi.fn(), change = vi.fn();
  render(<SnapshotReviewDialog open onOpenChange={change} repository="owner/repo" canChooseProfile={false} profileId="" onProfileChange={vi.fn()} capture={false} onCaptureChange={vi.fn()} busy={false} onError={vi.fn()} onConfirm={confirm} />);
  expect(screen.getByRole("dialog", {name:"复查此版本"})).toHaveAttribute("data-slot", "dialog-content");
  expect(screen.queryByLabelText("本次试跑方案")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", {name:"取消"}));
  expect(change).toHaveBeenCalledWith(false);
  expect(confirm).not.toHaveBeenCalled();
  await userEvent.keyboard("{Escape}");
  expect(confirm).not.toHaveBeenCalled();
});

it("提交中禁止关闭与重复提交，留存复选框保持禁用", async () => {
  const confirm = vi.fn(), change = vi.fn();
  render(<SnapshotReviewDialog open onOpenChange={change} repository="owner/repo" canChooseProfile={false} profileId="" onProfileChange={vi.fn()} capture onCaptureChange={vi.fn()} busy onError={vi.fn()} onConfirm={confirm} />);
  expect(screen.getByRole("checkbox")).toBeDisabled();
  expect(screen.getByRole("button", {name:"正在创建…"})).toBeDisabled();
  await userEvent.keyboard("{Escape}");
  expect(change).not.toHaveBeenCalled();
  expect(confirm).not.toHaveBeenCalled();
});
