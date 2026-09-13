"""模型数据外发策略。MVP 对受限内容拒绝整个请求，不截断代码或改变行号。"""

from fnmatch import fnmatchcase
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EgressPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    blocked_paths: tuple[str, ...] = Field(default=(
        ".env", ".env.*", "**/.env", "**/.env.*", "*.pem", "**/*.pem",
        "*.key", "**/*.key",
    ), max_length=50)
    allowed_hosts: tuple[str, ...] = Field(default=(), max_length=20)
    block_secrets: bool = True

    @field_validator("blocked_paths")
    @classmethod
    def paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value or len(value) > 256 or value.startswith("/")
               or ".." in value.split("/") or "\\" in value or ":" in value
               or any(ord(c) < 32 for c in value) for value in values):
            raise ValueError("外发路径必须是相对路径或通配符，不允许上级目录")
        return tuple(dict.fromkeys(values))

    @field_validator("allowed_hosts")
    @classmethod
    def hosts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.lower().rstrip(".") for value in values)
        if any(not value or len(value) > 253 or urlsplit(f"https://{value}").hostname != value
               or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in value)
               for value in normalized):
            raise ValueError("供应商白名单只填写完整域名，不含协议、端口或通配符")
        return tuple(dict.fromkeys(normalized))

    def denies_path(self, path: str) -> bool:
        return any(fnmatchcase(path.casefold(), pattern.casefold()) for pattern in self.blocked_paths)
