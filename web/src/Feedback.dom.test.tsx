// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DetailDialog, Notice } from "./Feedback";
import { readAppView } from "./App";

afterEach(() => {cleanup(); vi.useRealTimers(); document.body.style.overflow = "";});

it("通知独立于正文，自动消失且切页立即清理", () => {
  vi.useFakeTimers();
  const view = render(<main><Notice kind="success">保存成功</Notice><p>正文</p></main>);
  expect(view.container.querySelector(".app-notice")).toBeNull();
  expect(screen.getByRole("status")).toHaveTextContent("保存成功");
  act(() => vi.advanceTimersByTime(4500));
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  view.rerender(<main><Notice>保存失败</Notice><p>正文</p></main>);
  expect(screen.getByRole("alert")).toBeInTheDocument();
  act(() => window.dispatchEvent(new Event("hashchange")));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("详情按需打开，嵌套关闭和切页恢复滚动，保留原页面", () => {
  document.body.style.overflow = "auto";
  const toggle = vi.fn();
  render(<DetailDialog onToggle={toggle}><summary>查询详情</summary><p>保存的代码</p><DetailDialog><summary>内部信息</summary><p>内部代码</p></DetailDialog></DetailDialog>);
  expect(screen.queryByText("保存的代码")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", {name:/查询详情/}));
  expect(screen.getByRole("dialog", {name:"查询详情"})).toBeInTheDocument();
  expect(document.body.style.overflow).toBe("hidden");
  fireEvent.click(screen.getByRole("button", {name:/内部信息/}));
  fireEvent.click(screen.getAllByRole("button", {name:"关闭详情"})[1]);
  expect(document.body.style.overflow).toBe("hidden");
  act(() => window.dispatchEvent(new Event("hashchange")));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(document.body.style.overflow).toBe("auto");
  expect(toggle).toHaveBeenLastCalledWith({currentTarget:{open:false}});
  expect(screen.getByRole("button", {name:/查询详情/})).toHaveFocus();
});

it("预算链接保留仓库名称与目标设置", () => {
  expect(readAppView("#team?repository=owner%2FRepo&section=budget")).toEqual({kind:"team", repository:"owner/Repo", section:"budget"});
});
