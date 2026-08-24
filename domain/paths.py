"""所有不可信输入共用的仓库相对路径校验。"""

from pathlib import Path, PurePosixPath, PureWindowsPath


class RepositoryPathError(ValueError):
    """路径不是安全的仓库相对路径。"""


def normalize_repository_path(value: str) -> str:
    """返回跨平台仓库路径，并拒绝绝对路径或目录穿越形式。"""

    if not value or any(ord(character) < 32 for character in value):
        raise RepositoryPathError("file must be a repository-relative path")
    normalized = value.replace("\\", "/")
    windows_path = PureWindowsPath(value)
    posix_path = PurePosixPath(normalized)
    if (
        windows_path.drive
        or windows_path.root
        or posix_path.is_absolute()
        or normalized.startswith("//")
    ):
        raise RepositoryPathError("file must be a repository-relative path")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise RepositoryPathError("file must be a repository-relative path")
    return "/".join(parts)


def resolve_repository_path(
    repository_root: Path,
    relative_path: str,
    *,
    must_exist: bool = True,
) -> Path:
    """解析已校验路径，并强制限制在真实仓库边界内。"""

    root = repository_root.resolve(strict=True)
    normalized = normalize_repository_path(relative_path)
    candidate = root.joinpath(*normalized.split("/"))
    try:
        resolved = candidate.resolve(strict=must_exist)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise RepositoryPathError(
            "file must resolve inside the repository root"
        ) from exc
    return resolved
