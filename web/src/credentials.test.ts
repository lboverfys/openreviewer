import { afterEach, describe, expect, it, vi } from "vitest";

import { loadSavedCredentials, saveCredentials } from "./credentials";

afterEach(() => {
  // 每个用例都还原全局浏览器对象，避免一个模拟环境污染后续用例。
  vi.unstubAllGlobals();
});

describe("浏览器凭据适配层", () => {
  it("浏览器不支持凭据 API 时保持可登录且不写入本地存储", async () => {
    /** 验证兼容分支不会因为缺少 `navigator.credentials` 抛异常。 */
    vi.stubGlobal("navigator", {});
    vi.stubGlobal("window", {});

    await expect(loadSavedCredentials()).resolves.toBeNull();
    await expect(saveCredentials("admin", "secret")).resolves.toBe(false);
  });

  it("可以读取浏览器返回的账号密码对象", async () => {
    /** 验证读取结果只映射标准 `id/password` 字段，不依赖浏览器私有属性。 */
    const get = vi.fn().mockResolvedValue({ id: "admin", password: "secret" });
    vi.stubGlobal("navigator", { credentials: { get } });

    await expect(loadSavedCredentials()).resolves.toEqual({
      username: "admin",
      password: "secret",
    });
    expect(get).toHaveBeenCalledWith({ password: true, mediation: "optional" });
  });

  it("可以把新凭据交给浏览器密码库", async () => {
    /** 验证保存动作构造标准凭据对象，并把浏览器异常转换为 false。 */
    class FakePasswordCredential {
      readonly id: string;
      readonly password: string;
      readonly name?: string;

      constructor(data: { id: string; password: string; name?: string }) {
        this.id = data.id;
        this.password = data.password;
        this.name = data.name;
      }
    }
    const store = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { credentials: { store } });
    vi.stubGlobal("window", { PasswordCredential: FakePasswordCredential });

    await expect(saveCredentials("admin", "secret")).resolves.toBe(true);
    expect(store).toHaveBeenCalledOnce();
    expect(store.mock.calls[0][0]).toMatchObject({
      id: "admin",
      password: "secret",
      name: "admin",
    });
  });
});
