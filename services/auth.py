"""密码校验、签名会话与登录限流。"""

from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
from threading import Lock

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


class AuthConfigurationError(RuntimeError):
    """必需的认证配置缺失或无效。"""


class InvalidSessionError(ValueError):
    """浏览器会话缺失、无效或已经过期。"""


class LoginRateLimitError(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        """创建登录限流异常并保存建议等待时间。

        参数：
            retry_after_seconds: 当前限流窗口解除前至少还要等待的整秒数。API
                会把它写入 HTTP ``Retry-After`` 响应头。

        该异常只携带限流元数据，不包含用户名、密码或客户端地址，避免错误日志
        因直接记录异常对象而泄露登录输入。
        """
        super().__init__("too many failed login attempts")
        self.retry_after_seconds = retry_after_seconds


def _read_setting_or_file(
    values: Mapping[str, str],
    direct_name: str,
    file_name: str,
) -> str:
    """从直接配置或文件配置中读取一个敏感设置。

    直接值和文件路径是互斥的；读取文件时只保留去掉首尾空白后的内容。将
    文件读取集中到这里，既支持容器 secret 挂载，也避免认证配置在不同字段
    上出现不一致的优先级规则。

    参数：
        values: 环境变量名到字符串值的只读映射，测试可以传入普通字典。
        direct_name: 直接保存敏感值的配置名。
        file_name: 保存“敏感值文件路径”的配置名。

    返回：
        直接配置或 UTF-8 文件内容去除首尾空白后的字符串；两处都未配置时
        返回空字符串，由具体配置项的上层校验决定是否允许为空。

    异常：
        AuthConfigurationError: 两种来源同时配置，或指定文件无法读取。

    此函数不会修改环境变量，也不会把读取到的内容写入日志或其他文件。
    """
    direct_value = values.get(direct_name, "")
    path_value = values.get(file_name, "").strip()
    if direct_value and path_value:
        raise AuthConfigurationError(
            f"configure only one of {direct_name} and {file_name}"
        )
    if path_value:
        try:
            return Path(path_value).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AuthConfigurationError(f"{file_name} could not be read") from exc
    return direct_value.strip()


def _environment_boolean(values: Mapping[str, str], name: str, default: bool) -> bool:
    """把环境变量中的常见布尔写法转换为 ``bool``。

    未配置时使用传入的默认值；无法识别的字符串直接报配置错误，避免把拼写
    错误静默当成 ``False``，从而意外关闭安全选项。

    参数：
        values: 配置映射。
        name: 要读取的环境变量名。
        default: 变量完全不存在时采用的布尔值。

    返回：
        ``1/true/yes/on`` 对应 ``True``，``0/false/no/off`` 对应 ``False``；
        比较时忽略大小写和首尾空白。

    异常：
        AuthConfigurationError: 变量存在，但值不属于上述任一集合。
    """
    raw_value = values.get(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise AuthConfigurationError(f"{name} must be true or false")


@dataclass(frozen=True, slots=True)
class AuthSettings:
    username: str
    password_hash: str
    session_secret: bytes
    cookie_secure: bool = True
    session_ttl: timedelta = timedelta(hours=8)

    @property
    def cookie_name(self) -> str:
        """返回与传输安全设置匹配的会话 Cookie 名称。

        HTTPS 模式使用 ``__Host-`` 前缀，浏览器会强制该 Cookie 只属于当前主机
        且不能设置 Domain；本地非 HTTPS 测试则使用普通名称，避免浏览器拒收。

        返回：
            ``cookie_secure=True`` 时返回
            ``__Host-openreviewer_session``，否则返回 ``openreviewer_session``。

        该属性只决定名称；``Secure``、``HttpOnly``、``SameSite`` 和 ``Path``
        等属性由 API 设置 Cookie 时一并指定。
        """
        return (
            "__Host-openreviewer_session"
            if self.cookie_secure
            else "openreviewer_session"
        )

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "AuthSettings":
        """从环境映射加载并校验管理员认证配置。

        必须提供用户名、Argon2id 密码哈希和至少 32 字节的会话签名密钥；密码
        只接受哈希，不接受明文。会话 TTL、Secure Cookie 开关也在这里统一解析，
        这样 API 和测试实例使用同一套边界规则。

        参数：
            environment: 可选配置映射；不传时读取当前进程的 ``os.environ``。
                传入映射主要用于测试，方法不会修改它。

        返回：
            完成清理和类型转换的不可变 ``AuthSettings``。默认会话时长为 8 小时，
            允许范围是 5 分钟到 24 小时。

        异常：
            AuthConfigurationError: 用户名为空或过长、密码不是 Argon2id 哈希、
            会话密钥不足 32 字节、TTL 不是合法范围内整数，或布尔配置无法识别。

        密码哈希和会话密钥都支持“直接值”与“文件路径”两种互斥来源；该方法
        只读取配置，不建立数据库连接，也不校验管理员的明文密码。
        """
        values = os.environ if environment is None else environment
        username = values.get("OPENREVIEWER_ADMIN_USERNAME", "").strip()
        if not 1 <= len(username) <= 100:
            raise AuthConfigurationError(
                "OPENREVIEWER_ADMIN_USERNAME must contain 1 to 100 characters"
            )

        password_hash = _read_setting_or_file(
            values,
            "OPENREVIEWER_ADMIN_PASSWORD_HASH",
            "OPENREVIEWER_ADMIN_PASSWORD_HASH_FILE",
        )
        if not password_hash.startswith("$argon2id$"):
            raise AuthConfigurationError("the administrator password must use Argon2id")

        session_secret = _read_setting_or_file(
            values,
            "OPENREVIEWER_SESSION_SECRET",
            "OPENREVIEWER_SESSION_SECRET_FILE",
        ).encode("utf-8")
        if len(session_secret) < 32:
            raise AuthConfigurationError(
                "the session signing secret must contain at least 32 bytes"
            )

        ttl_raw = values.get("OPENREVIEWER_SESSION_TTL_SECONDS", "28800")
        try:
            ttl_seconds = int(ttl_raw)
        except ValueError as exc:
            raise AuthConfigurationError(
                "OPENREVIEWER_SESSION_TTL_SECONDS must be an integer"
            ) from exc
        if not 300 <= ttl_seconds <= 86400:
            raise AuthConfigurationError(
                "OPENREVIEWER_SESSION_TTL_SECONDS must be between 300 and 86400"
            )

        return cls(
            username=username,
            password_hash=password_hash,
            session_secret=session_secret,
            cookie_secure=_environment_boolean(
                values,
                "OPENREVIEWER_COOKIE_SECURE",
                True,
            ),
            session_ttl=timedelta(seconds=ttl_seconds),
        )


@dataclass(frozen=True, slots=True)
class SessionPrincipal:
    username: str
    issued_at: datetime
    expires_at: datetime


class AuthService:
    def __init__(
        self,
        settings: AuthSettings,
        *,
        password_hasher: PasswordHasher | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """创建认证服务。

        密码哈希器和时钟可以注入低成本测试实现；生产环境默认使用 Argon2 的
        ``PasswordHasher`` 和当前 UTC 时间。服务本身不保存明文密码，也不把会话
        Token 持久化到数据库。

        参数：
            settings: 已校验的管理员账号、密码哈希、签名密钥和 Cookie 配置。
            password_hasher: 可选的 Argon2 校验器；不传时创建生产用实现。
            clock: 可选时钟函数；不传时读取当前 UTC 时间。测试可注入固定时钟。

        构造过程没有网络和数据库副作用。传入对象会被保存供后续登录与会话校验使用。
        """
        self.settings = settings
        self._password_hasher = password_hasher or PasswordHasher()
        self._clock = clock or (lambda: datetime.now(UTC))

    def verify_credentials(self, username: str, password: str) -> bool:
        """同时校验管理员用户名和密码，不暴露哪一项不匹配。

        参数：
            username: 登录请求中的用户名，按原样参与常量时间比较。
            password: 登录请求中的明文密码，只在本次 Argon2 校验期间使用。

        返回：
            用户名和密码都匹配时返回 ``True``；任一项错误或密码哈希无效时
            返回 ``False``。

        即使用户名明显错误，方法仍会执行一次密码哈希验证，避免“未知账号很快、
        已知账号较慢”的时间差帮助攻击者枚举账号。Argon2 的常见校验异常会在这里
        转成 ``False``，不会把哈希内容或具体失败原因交给 API。
        """

        try:
            password_matches = self._password_hasher.verify(
                self.settings.password_hash,
                password,
            )
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            password_matches = False
        username_matches = hmac.compare_digest(username, self.settings.username)
        return bool(password_matches and username_matches)

    def create_session(self) -> tuple[str, SessionPrincipal]:
        """创建一个带过期时间和随机 nonce 的签名会话 Token。

        Token 由版本、Base64URL 编码的 JSON 负载和 HMAC-SHA256 签名组成。服务端
        不需要存储每个 Token；只要签名密钥未泄露，就能在收到请求时验证完整性和
        有效期。返回值同时包含前端需要展示的会话主体信息。

        返回：
            二元组的第一项是应写入 HttpOnly Cookie 的签名 Token；第二项是包含
            用户名、签发时间和到期时间的 ``SessionPrincipal``。

        负载中的随机 ``nonce`` 让同一用户在同一秒登录两次也得到不同 Token。
        本方法只在内存中组装结果，不写数据库或文件；退出登录通过浏览器删除
        Cookie 实现，服务端当前没有单个 Token 吊销表。
        """
        issued_at = self._clock().astimezone(UTC)
        expires_at = issued_at + self.settings.session_ttl
        payload = {
            "v": 1,
            "sub": self.settings.username,
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
            "nonce": secrets.token_urlsafe(16),
        }
        encoded_payload = self._encode(
            json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        signed_data = f"v1.{encoded_payload}".encode("ascii")
        signature = hmac.new(
            self.settings.session_secret,
            signed_data,
            hashlib.sha256,
        ).digest()
        token = f"v1.{encoded_payload}.{self._encode(signature)}"
        return token, SessionPrincipal(
            username=self.settings.username,
            issued_at=issued_at,
            expires_at=expires_at,
        )

    def verify_session(self, token: str | None) -> SessionPrincipal:
        """验证会话 Cookie 的格式、签名、主体和时间窗口。

        先验证 HMAC，再解析 JSON，防止攻击者修改用户名或过期时间。还会拒绝
        未开始、已过期、版本不支持或 Base64 非规范编码的 Token；所有格式问题
        都统一转换为 ``InvalidSessionError``，API 层可以稳定返回 401。

        参数：
            token: 从会话 Cookie 读取的完整 Token；缺失 Cookie 时传入 ``None``。

        返回：
            签名、版本、主体和时间窗口全部有效时，返回可信的会话主体。

        异常：
            InvalidSessionError: Token 缺失、段数或编码错误、签名不匹配、主体
            不是当前管理员、已到期，或签发时间比服务器时间超前一分钟以上。

        允许一分钟的未来时间是为轻微时钟偏差留余量；此方法不会刷新会话期限，
        也不会修改 Cookie 或任何服务端状态。
        """
        if not token:
            raise InvalidSessionError("session cookie is missing")
        try:
            version, encoded_payload, encoded_signature = token.split(".")
            if version != "v1":
                raise InvalidSessionError("unsupported session version")
            signed_data = f"{version}.{encoded_payload}".encode("ascii")
            expected_signature = hmac.new(
                self.settings.session_secret,
                signed_data,
                hashlib.sha256,
            ).digest()
            supplied_signature = self._decode(encoded_signature)
            if not hmac.compare_digest(expected_signature, supplied_signature):
                raise InvalidSessionError("session signature is invalid")

            payload = json.loads(self._decode(encoded_payload))
            if (
                not isinstance(payload, dict)
                or payload.get("v") != 1
                or payload.get("sub") != self.settings.username
                or not isinstance(payload.get("iat"), int)
                or not isinstance(payload.get("exp"), int)
            ):
                raise InvalidSessionError("session payload is invalid")
            issued_at = datetime.fromtimestamp(payload["iat"], UTC)
            expires_at = datetime.fromtimestamp(payload["exp"], UTC)
            now = self._clock().astimezone(UTC)
            if expires_at <= now or issued_at > now + timedelta(minutes=1):
                raise InvalidSessionError("session is expired or not active")
            return SessionPrincipal(
                username=self.settings.username,
                issued_at=issued_at,
                expires_at=expires_at,
            )
        except InvalidSessionError:
            raise
        except (ValueError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
            raise InvalidSessionError("session token is malformed") from exc

    @staticmethod
    def _encode(value: bytes) -> str:
        """把任意字节编码成 Token 可用的规范 Base64URL 文本。

        参数：
            value: JSON 负载或 HMAC 签名的原始字节。

        返回：
            使用 URL 安全字符且移除末尾 ``=`` 填充的 ASCII 字符串。

        去除填充只改变文本表示，不丢失信息；解码时会按长度恢复所需填充。
        """
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode(value: str) -> bytes:
        """严格解码 Token 中的一段 Base64URL 文本。

        参数：
            value: 不带 ``=`` 填充的 Base64URL 字符串。

        返回：
            解码后的原始字节。

        异常：
            ValueError: 文本不是规范表示，例如包含非法字符、显式多余填充，
            或同一字节值使用了不同编码形式。

        方法会先补齐解码所需填充，再重新编码并做常量时间比较，确保只接受
        :meth:`_encode` 能产生的唯一形式。
        """
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
        if not hmac.compare_digest(AuthService._encode(decoded), value):
            raise ValueError("base64 value is not canonically encoded")
        return decoded


class LoginAttemptLimiter:
    """适用于当前单 API 副本的进程内滑动窗口限流器。"""

    def __init__(
        self,
        *,
        maximum_failures: int = 5,
        window: timedelta = timedelta(minutes=15),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """初始化进程内登录失败滑动窗口。

        当前只有一个 API 副本，因此用带锁的内存队列即可限制同一客户端/账号的
        连续失败次数；如果未来横向扩容，需要把这个边界替换为共享存储。

        参数：
            maximum_failures: 一个窗口内允许记录的最大失败次数；达到该数量后
                后续 ``check`` 会拒绝登录。
            window: 滑动时间窗口，默认 15 分钟。
            clock: 可选时钟函数，测试可用它精确推进窗口时间。

        异常：
            ValueError: 最大失败次数不为正数，或窗口长度不大于零。

        记录只存在当前 API 进程内，进程重启会清空；``Lock`` 用来保证并发请求
        更新同一键时不会丢失计数。
        """
        if maximum_failures <= 0 or window.total_seconds() <= 0:
            raise ValueError("login limiter values must be positive")
        self._maximum_failures = maximum_failures
        self._window = window
        self._clock = clock or (lambda: datetime.now(UTC))
        self._failures: dict[str, deque[datetime]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str) -> None:
        """检查某个客户端和账号是否仍允许尝试登录。

        先清理窗口外的失败记录；达到上限时抛出包含剩余等待秒数的异常，调用方
        应将其转换为 HTTP 429，而不是继续执行密码校验。

        参数：
            key: API 组合出的限流身份，当前由客户端地址和大小写折叠后的用户名
            组成。调用方必须保证同一身份始终使用相同格式。

        异常：
            LoginRateLimitError: 活跃失败数已达到上限；异常中包含最早失败记录
            退出窗口前的剩余秒数。

        校验本身不会增加失败次数；只有实际凭据校验失败后才应调用
        :meth:`record_failure`。
        """
        with self._lock:
            failures = self._active_failures(key)
            if len(failures) < self._maximum_failures:
                return
            retry_at = failures[0] + self._window
            remaining = max(1, int((retry_at - self._clock()).total_seconds()) + 1)
            raise LoginRateLimitError(remaining)

    def record_failure(self, key: str) -> None:
        """把当前时刻的一次认证失败记入滑动窗口。

        参数：
            key: 与调用 :meth:`check` 时完全相同的客户端/账号组合键。

        副作用：
            在线程锁保护下清除过期记录，再把当前时钟值追加到内存队列。方法
            不负责判断是否已超限，也不持久化记录。
        """
        with self._lock:
            failures = self._active_failures(key)
            failures.append(self._clock())

    def reset(self, key: str) -> None:
        """清除某个限流键的全部失败历史。

        参数：
            key: 已成功登录的客户端/账号组合键。

        副作用：
            从当前进程的内存字典删除该键。键不存在时保持幂等，不会抛错；其他
            客户端或账号的失败记录不受影响。
        """
        with self._lock:
            self._failures.pop(key, None)

    def _active_failures(self, key: str) -> deque[datetime]:
        """返回窗口内仍有效的失败队列，并删除过期时间点。

        调用者必须已经持有 ``_lock``；这个内部方法不重复加锁，避免在锁已持有
        时发生不可重入锁死。

        参数：
            key: 要读取和清理的限流键。

        返回：
            字典中属于该键的可变 ``deque``。返回对象仍由内部状态持有，调用者
            只能在锁内读取或追加。

        副作用：
            删除时间小于等于窗口截止点的记录。清空后会重建空队列，以便调用方
            可以直接追加而无需再次判断键是否存在。
        """
        failures = self._failures[key]
        cutoff = self._clock() - self._window
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            self._failures.pop(key, None)
            failures = self._failures[key]
        return failures
