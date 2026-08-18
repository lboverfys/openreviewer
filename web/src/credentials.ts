/**
 * 浏览器凭据管理器适配层。
 *
 * 登录凭据属于浏览器密码库应该负责的敏感数据，而不是 React 状态或
 * `localStorage` 应用数据。这里把 Credential Management API 的特性检测、
 * 类型兼容和异常吞掉集中起来，让登录组件只关心“读到/保存了什么”。
 *
 * 需要注意：不同浏览器、无痕模式、非安全来源和浏览器策略都可能关闭该 API。
 * 在这些情况下方法会返回空结果或 `false`，调用方仍可以正常手动登录，不会因为
 * 自动保存能力不可用而阻断认证流程。
 */

interface PasswordCredentialLike {
  readonly id: string;
  readonly password: string;
}

interface PasswordCredentialConstructorLike {
  new (data: { id: string; password: string; name?: string }): PasswordCredentialLike;
}

interface CredentialsContainerLike {
  get(options: {
    password: boolean;
    mediation?: "silent" | "optional" | "required";
  }): Promise<unknown>;
  store(credential: unknown): Promise<unknown>;
}

interface CredentialWindow extends Window {
  PasswordCredential?: PasswordCredentialConstructorLike;
}

interface CredentialNavigatorLike {
  credentials?: CredentialsContainerLike;
}

export interface SavedCredentials {
  /** 浏览器密码库中保存的账号。 */
  username: string;
  /** 浏览器密码库中保存的密码。 */
  password: string;
}

function isPasswordCredential(value: unknown): value is PasswordCredentialLike {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<PasswordCredentialLike>;
  return typeof candidate.id === "string" && typeof candidate.password === "string";
}

/**
 * 从浏览器密码库读取最近可用的账号密码。
 *
 * 使用 `mediation: "optional"` 请求普通的自动填充：浏览器有已授权凭据时直接
 * 返回，没有时返回 `null`，不会强制弹出账号选择器。浏览器返回的对象只在当前
 * 页面运行期间交给 React，应用不会把它序列化到本地存储或发送到日志系统。
 */
export async function loadSavedCredentials(): Promise<SavedCredentials | null> {
  if (typeof navigator === "undefined") return null;
  const credentialNavigator = navigator as unknown as CredentialNavigatorLike;
  const credentialStore = credentialNavigator.credentials;
  if (!credentialStore?.get) return null;

  try {
    const credential = await credentialStore.get({
      password: true,
      mediation: "optional",
    });
    if (!isPasswordCredential(credential)) return null;
    if (!credential.id || !credential.password) return null;
    return { username: credential.id, password: credential.password };
  } catch {
    // 浏览器策略拒绝读取时保持普通手动登录，不把实现细节暴露给用户。
    return null;
  }
}

/**
 * 请求浏览器把本次成功登录保存到密码库。
 *
 * 返回值区分“浏览器接受保存请求”和“当前环境不支持/拒绝”。保存失败不会影响
 * 已经成功建立的服务端会话；用户仍可使用浏览器自身的密码管理界面补录凭据。
 */
export async function saveCredentials(
  username: string,
  password: string,
): Promise<boolean> {
  if (typeof navigator === "undefined" || typeof window === "undefined") {
    return false;
  }
  const credentialNavigator = navigator as unknown as CredentialNavigatorLike;
  const Credential = (window as CredentialWindow).PasswordCredential;
  const credentialStore = credentialNavigator.credentials;
  if (!Credential || !credentialStore?.store) return false;

  try {
    const credential = new Credential({
      id: username,
      password,
      name: username,
    });
    await credentialStore.store(credential);
    return true;
  } catch {
    // 保存权限、来源安全策略或浏览器实现差异不应让登录失败。
    return false;
  }
}
